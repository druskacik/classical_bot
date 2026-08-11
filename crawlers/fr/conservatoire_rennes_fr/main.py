import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://conservatoire-rennes.fr/'
SOURCE = 'Conservatoire de Rennes'
SITEMAP_URL = f'{SOURCE_URL}evts-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
    'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    urls = [clean_text(node) for node in soup.select('url > loc')]
    return [url for url in urls if url.rstrip('/') != f'{SOURCE_URL}agenda']


def published_date(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = payload.get('@graph', []) if isinstance(payload, dict) else []
        for node in nodes:
            value = node.get('datePublished') if isinstance(node, dict) else None
            if value:
                try:
                    return date.fromisoformat(value[:10])
                except ValueError:
                    pass
    return None


def resolve_date(day, month_name, published):
    month = MONTHS.get(clean_text(month_name).casefold())
    if not month or not published:
        return None
    # Events are normally published during the preceding part of the same
    # September-to-June season. This also handles autumn events published in
    # the preceding spring by considering the closest future occurrence.
    year = published.year
    try:
        candidate = date(year, month, int(clean_text(day)))
    except (TypeError, ValueError):
        return None
    if candidate < published:
        try:
            candidate = candidate.replace(year=year + 1)
        except ValueError:
            return None
    return candidate.isoformat()


def resolve_city(soup, venue_block):
    venue_text = clean_text(venue_block)
    if re.search(r'\bRennes\b', venue_text, re.IGNORECASE):
        return 'Rennes'

    # Nearly all listings are in Rennes. Apply that institutional default only
    # when the first-party map coordinates independently place the venue in the
    # Rennes urban area; touring records outside it are skipped unless named.
    map_node = soup.select_one('#mapSidebar[data-lat][data-lng]')
    if map_node:
        try:
            lat = float(map_node.get('data-lat'))
            lng = float(map_node.get('data-lng'))
            if 47.95 <= lat <= 48.25 and -1.85 <= lng <= -1.45:
                return 'Rennes'
        except (TypeError, ValueError):
            pass
    return None


def parse_page(session, url):
    soup = BeautifulSoup(get_response(session, url).content, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    published = published_date(soup)

    venue_node = None
    for block in soup.select('.side-contact-infos:not(.side-contact-heures)'):
        if block.select_one('strong'):
            venue_node = block.select_one('strong')
            break
    venue_lines = clean_text(venue_node).splitlines()
    venue = venue_lines[0].strip() if venue_lines else ''
    city = resolve_city(soup, venue_node)

    time_node = soup.select_one('.side-contact-heures strong')
    time_match = re.search(r'\b([01]?\d|2[0-3])h([0-5]\d)\b', clean_text(time_node))
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None

    description_parts = []
    for node in soup.select('.page-structure .md-8 .paragraph'):
        text = clean_text(node)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    if not title or not venue or not city:
        return []

    records = []
    for date_node in soup.select('.agenda-dates .agenda-date'):
        event_date = resolve_date(
            clean_text(date_node.select_one('.day')),
            clean_text(date_node.select_one('.month')),
            published,
        )
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


class ConservatoireRennesCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='conservatoire_rennes_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = event_urls(session)
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(parse_page, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Conservatoire de Rennes event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return records


def main():
    return ConservatoireRennesCrawler().run()


if __name__ == '__main__':
    main()
