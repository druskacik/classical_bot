import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://simfonicadebalears.com/'
SOURCE = 'Orquestra Simfònica de les Illes Balears'
SITEMAP_URL = f'{SOURCE_URL}ajde_events-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ca-ES,ca;q=0.9,es;q=0.8',
}

# EventON stores most addresses as a single streetAddress string instead of a
# structured addressLocality. These are the municipalities used by the source.
CITY_NAMES = {
    'alcudia': 'Alcúdia',
    'capdepera': 'Capdepera',
    'eivissa': 'Eivissa',
    'ibiza': 'Eivissa',
    'inca': 'Inca',
    'llucmajor': 'Llucmajor',
    'manacor': 'Manacor',
    'mao': 'Maó',
    'mahon': 'Maó',
    'palma': 'Palma',
    'pollenca': 'Pollença',
    'sant lluis': 'Sant Lluís',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def ascii_key(value):
    return str(value).casefold().translate(str.maketrans({
        'à': 'a', 'á': 'a', 'è': 'e', 'é': 'e', 'í': 'i',
        'ï': 'i', 'ò': 'o', 'ó': 'o', 'ú': 'u', 'ü': 'u', 'ç': 'c',
    }))


def infer_city(location, address):
    if isinstance(address, dict):
        locality = clean_text(address.get('addressLocality'))
        if locality:
            return locality
        address = address.get('streetAddress', '')
    evidence = ascii_key(f'{location} {address}')
    # Prefer longer names so "Sant Lluís" is not accidentally reduced.
    for needle in sorted(CITY_NAMES, key=len, reverse=True):
        if re.search(rf'(?<!\w){re.escape(needle)}(?!\w)', evidence):
            return CITY_NAMES[needle]
    return None


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        if isinstance(value, dict) and isinstance(value.get('@graph'), list):
            candidates.extend(value['@graph'])
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    # The sitemap contains Catalan, Spanish, and English copies of each event.
    # Keep the canonical Catalan record so translations do not become duplicates.
    if not soup.html or not soup.html.get('lang', '').lower().startswith('ca'):
        return None
    event = event_schema(soup)
    if not event:
        return None

    title = clean_text(event.get('name'))
    start = clean_text(event.get('startDate'))
    match = re.match(
        r'^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2}))?',
        start,
    )
    if not match:
        return None
    try:
        year, month, day = (int(match.group(index)) for index in range(1, 4))
        event_date = date(year, month, day).isoformat()
    except ValueError:
        return None

    locations = event.get('location') or []
    if isinstance(locations, dict):
        locations = [locations]
    location = next((item for item in locations if isinstance(item, dict)), {})
    venue = clean_text(location.get('name'))
    address = location.get('address') or {}
    city = infer_city(venue, address)
    canonical = soup.select_one('link[rel="canonical"]')
    event_url = canonical.get('href', '').strip() if canonical else url

    description_html = event.get('description')
    description = clean_text(BeautifulSoup(description_html, 'html.parser')) if description_html else None
    if not all((title, event_date, event_url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': (
            f'{int(match.group(4)):02d}:{match.group(5)}'
            if match.group(4) else None
        ),
        'venue': venue,
        'city': city,
        'description': description or None,
    }


def sitemap_event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    return sorted({
        node.get_text(strip=True)
        for node in soup.find_all('loc')
        if '/events/' in node.get_text()
        and node.get_text(strip=True).rstrip('/') != f'{SOURCE_URL.rstrip("/")}/events'
        and not re.search(
            r'(?:-cast|-eng)/?$|/(?:[^/]*concert-season|[^/]*concierto|film-music|'
            r'musica-de-cine-con|new-year-concert|concert-centenary)',
            node.get_text(strip=True),
        )
    })


class SimfonicaDeBalearsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='simfonicadebalears_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(SITEMAP_URL, timeout=45)
        response.raise_for_status()
        urls = sitemap_event_urls(response.text)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    item_response = future.result()
                    item_response.raise_for_status()
                    record = parse_event_page(item_response.text, url)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Simfònica de les Illes Balears event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    SimfonicaDeBalearsCrawler().run()


if __name__ == '__main__':
    main()
