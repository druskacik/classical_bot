import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lanaudiere.org/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Festival de Lanaudière'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CA,fr;q=0.9,en;q=0.7',
}

# The concert CMS exposes a venue name but no postal address. These are the
# festival's published venues; keeping the mapping explicit prevents a home-city
# default from being incorrectly applied to concerts elsewhere in Lanaudiere.
VENUE_CITIES = {
    'Amphithéâtre Fernand-Lindsay': 'Joliette',
    'CRAPO de Lanaudière': 'Saint-Jean-de-Matha',
    'Distillerie Grand Dérangement': 'Saint-Jacques',
    'Espace Culturel Jean-Pierre Ferland': 'Saint-Calixte',
    'Jardins Arômes et Saveurs': 'Saint-Jacques',
    'Musée d’art de Joliette': 'Joliette',
    'Place Bourget': 'Joliette',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def concert_urls(sitemap):
    soup = BeautifulSoup(sitemap, 'xml')
    urls = set()
    for node in soup.select('loc'):
        url = clean_text(node.get_text())
        path = urlparse(url).path.rstrip('/')
        if path.startswith('/concerts/') and path.count('/') == 2:
            urls.add(urljoin(SOURCE_URL, path))
    return sorted(urls)


def music_event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text() or '{}')
        except (TypeError, ValueError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'MusicEvent':
                return item
    return {}


def city_for_venue(venue):
    if venue in VENUE_CITIES:
        return VENUE_CITIES[venue]
    match = re.fullmatch(r'Église de (.+)', venue)
    if match:
        return match.group(1).replace('Saint-Ambroise', 'Saint-Ambroise-de-Kildare')
    return None


def description_from_page(soup, title, venue):
    main = soup.select_one('main')
    if not main:
        return None
    text = clean_text(main.get_text('\n', strip=True))
    text = re.split(r'\nCONCERTS RELI[ÉE]S\b', text, maxsplit=1, flags=re.IGNORECASE)[0]
    lines = text.splitlines()
    while lines and (
        lines[0].casefold() == title.casefold()
        or re.search(r'\d{1,2}\s+\w+\s+\d{4}\s+à\s+\d', lines[0], re.IGNORECASE)
        or lines[0] == venue
    ):
        lines.pop(0)
    return clean_text('\n'.join(lines)) or None


def make_record(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    event = music_event_data(soup)
    title = clean_text(event.get('name'))
    location = event.get('location') or {}
    venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
    city = city_for_venue(venue)
    start = event.get('startDate')
    if not title or not start or not venue or not city:
        return None
    try:
        starts_at = datetime.fromisoformat(start.replace('Z', '+00:00'))
        starts_at = starts_at.astimezone(ZoneInfo('America/Toronto'))
    except (TypeError, ValueError):
        return None
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'CA',
        'description': description_from_page(soup, title, venue),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(get_page(session, SITEMAP_URL))
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_page, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = make_record(url, future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped concert with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class LanaudiereOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lanaudiere_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LanaudiereOrgCrawler().run()


if __name__ == '__main__':
    main()
