import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://conservatoire.lille.fr/'
AGENDA_URL = f'{SOURCE_URL}agenda-et-reservation'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Conservatoire à Rayonnement Régional de Lille'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    elif '<' not in str(value) or '>' not in str(value):
        text = str(value)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = payload.get('@graph', []) if isinstance(payload, dict) else []
        if isinstance(payload, dict) and payload.get('@type') == 'Event':
            nodes = [payload]
        for node in nodes:
            node_types = node.get('@type', []) if isinstance(node, dict) else []
            if node_types == 'Event' or 'Event' in node_types:
                return node
    return None


def parse_datetime(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M') if 'T' in str(value) else None


def description_from_page(soup, schema):
    parts = []
    for node in (soup.select_one('main .lead'), soup.select_one('main .event-detail .a-edito')):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    schema_description = clean_text(schema.get('description'))
    if schema_description and schema_description not in parts:
        parts.insert(0, schema_description)
    return '\n\n'.join(parts) or None


def venue_from_event(description, address):
    text = description or ''
    patterns = (
        r'(?:📍|Lieu\s*:\s*)([^\n]+)',
        r'\b(Auditorium du Conservatoire(?: de Lille)?)\b',
        r'\b(Conservatoire de Lille)\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            venue = clean_text(match.group(1)).strip(' .,-')
            venue = re.split(r'\s[-–]\s|,\s*(?:Place|Rue|Boulevard|Avenue)\b', venue)[0]
            if venue:
                return venue

    street = clean_text(address.get('streetAddress')).lower()
    if 'place du concert' in street:
        return 'Conservatoire à Rayonnement Régional de Lille'
    return None


def record_from_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return None

    title = clean_text(schema.get('name'))
    event_date, time_from = parse_datetime(schema.get('startDate'))
    location = schema.get('location') or {}
    address = location.get('address') or {}
    city = clean_text(address.get('addressLocality'))
    country_code = clean_text(address.get('addressCountry')).upper()
    description = description_from_page(soup, schema)
    venue = clean_text(location.get('name')) or venue_from_event(description, address)
    canonical_url = clean_text(schema.get('url')) or url

    if not all((title, event_date, canonical_url, venue, city, country_code)):
        return None
    if not re.fullmatch(r'[A-Z]{2}', country_code):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': canonical_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return [clean_text(node).replace('http://', 'https://', 1) for node in soup.find_all('loc')]


def agenda_urls(session):
    urls = []
    page = 0
    while True:
        response = session.get(AGENDA_URL, params={'page': page}, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = [
            link['href']
            for link in soup.select('main a[href]')
            if clean_text(link).lower() == 'lire la suite'
        ]
        page_urls = [
            url if url.startswith('http') else requests.compat.urljoin(SOURCE_URL, url)
            for url in page_urls
        ]
        new_urls = [url for url in page_urls if url not in urls]
        if not new_urls:
            return urls
        urls.extend(new_urls)
        next_link = soup.select_one('a[rel="next"], .pager__item--next a')
        if not next_link:
            return urls
        page += 1


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    # The agenda is the authoritative current feed. The sitemap additionally
    # discovers any older event pages that remain published with Event schema.
    urls = list(dict.fromkeys(agenda_urls(session) + sitemap_urls(session)))
    records = []

    def fetch(url):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return record_from_page(response.url, response.text)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to inspect sitemap page',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class ConservatoireLilleFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='conservatoire_lille_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ConservatoireLilleFrCrawler().run()


if __name__ == '__main__':
    main()
