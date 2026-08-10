import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orquestadeextremadura.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/ajde_events'
SOURCE = 'Orquesta de Extremadura'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

# EventON omits addressLocality, but normally publishes the city either after
# the Spanish postal code or in the venue/touring-event name.
KNOWN_CITIES = (
    'Badajoz', 'Cáceres', 'Mérida', 'Plasencia', 'Don Benito', 'Villanueva de la Serena',
    'Almendralejo', 'Zafra', 'Trujillo', 'Guadalupe', 'Llerena', 'Olivenza', 'Baeza',
    'Madrid', 'Sevilla', 'Toledo', 'Salamanca', 'Valladolid', 'Lisboa',
)


def city_from_text(value):
    text = clean_text(value)
    postal = re.search(r'\b\d{5}\s+([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÜÑáéíóúüñ .-]+)$', text)
    if postal:
        return postal.group(1).strip(' .,')
    folded = text.casefold()
    return next((city for city in KNOWN_CITIES if city.casefold() in folded), None)


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_items(session):
    records = []
    page = 1
    while True:
        response = session.get(API_URL, params={'per_page': 100, 'page': page}, timeout=60)
        response.raise_for_status()
        records.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', 1)):
            return records
        page += 1


def json_events(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                yield candidate


def location_parts(event):
    location = event.get('location') or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    address = location.get('address') or {}
    if isinstance(address, str):
        address_text = clean_text(address)
        city = city_from_text(address_text)
    else:
        address_text = clean_text(address.get('streetAddress'))
        city = clean_text(address.get('addressLocality')) or city_from_text(address_text)
    venue = clean_text(location.get('name'))
    if not city:
        # EventON often stores the town in the venue or touring-event title
        # instead of the PostalAddress locality.
        city = city_from_text(' '.join((venue, clean_text(event.get('name')), address_text)))
    return venue, city


def make_record(event, fallback_url, fallback_description):
    title = clean_text(event.get('name'))
    url = clean_text(event.get('url')) or fallback_url
    start = clean_text(event.get('startDate'))
    venue, city = location_parts(event)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})(?:T(\d{1,2}):(\d{2}))?', start)
    if not match:
        return None
    try:
        start_at = datetime(*map(int, match.groups(default='0')))
    except ValueError:
        return None
    if not all((title, url, venue, city)):
        return None
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M') if 'T' in start else None,
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': clean_text(event.get('description')) or fallback_description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_item(session, item):
    url = item.get('link') or ''
    if not url:
        return []
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    description = clean_text((item.get('content') or {}).get('rendered'))
    return [
        record for event in json_events(soup)
        if (record := make_record(event, url, description)) is not None
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = api_items(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scrape_item, session, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class OrquestadeextremaduraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestadeextremadura_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OrquestadeextremaduraComCrawler().run()


if __name__ == '__main__':
    main()
