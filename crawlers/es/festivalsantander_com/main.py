import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://festivalsantander.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/programacion'
SOURCE = 'Festival Internacional de Santander'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'septiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}

SANTANDER_VENUES = {
    'sala argenta': 'Sala Argenta, Palacio de Festivales de Cantabria',
    'sala pereda': 'Sala Pereda, Palacio de Festivales de Cantabria',
    'centro botin': 'Centro Botín',
    'centro botín': 'Centro Botín',
    'faro cabo mayor': 'Faro de Cabo Mayor',
    'peninsula de la magdalena': 'Península de la Magdalena',
    'península de la magdalena': 'Península de la Magdalena',
    'palacio de festivales': 'Palacio de Festivales de Cantabria',
    'terraza de gamazo': 'Terraza de Gamazo',
}

# These venue-only headings are used consistently by the festival calendar.
VENUE_CITIES = {
    'santuario de la bien aparecida': 'Ampuero',
    'cueva el soplao': 'Celis',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def edition_year(item):
    for term_group in (item.get('_embedded') or {}).get('wp:term', []):
        for term in term_group:
            if term.get('taxonomy') != 'edicion':
                continue
            match = re.match(r'(\d+)', term.get('slug', ''))
            if match:
                # The first festival was held in 1952; e.g. edition 75 is 2026.
                return 1951 + int(match.group(1))
    match = re.search(r'/programacion/(\d+)-edicion/', item.get('link', ''))
    return 1951 + int(match.group(1)) if match else None


def parse_datetime(text, year):
    match = re.search(
        r'(\d{1,2})\s+de\s+([a-záéíóúñ]+)(?:\s+a\s+las\s+'
        r'(\d{1,2})[.:](\d{2}))?',
        text.lower(),
    )
    if not match or not year or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(3):
        hour, minute = int(match.group(3)), int(match.group(4))
        if hour < 24 and minute < 60:
            event_time = f'{hour:02d}:{minute:02d}'
    return event_date, event_time


def parse_location(text):
    parts = [part.strip(' .') for part in re.split(r'\s*/\s*', clean_text(text))]
    parts = [part for part in parts if part]
    generic = {'marcos históricos', 'palacio de festivales', 'centro botín'}

    for location in parts:
        if '.' in location:
            city, venue = (part.strip(' .') for part in location.split('.', 1))
            if city.lower() in SANTANDER_VENUES:
                return venue, 'Santander'
            if city.lower() not in generic and venue:
                return venue, city.title()

    for location in reversed(parts):
        normalized = location.lower()
        if normalized in SANTANDER_VENUES:
            return SANTANDER_VENUES[normalized], 'Santander'
        if normalized in VENUE_CITIES:
            return location, VENUE_CITIES[normalized]
    return None, None


def api_items(session):
    items = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, '_embed': 'wp:term'},
            timeout=60,
        )
        response.raise_for_status()
        items.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', 1))
        if page >= total_pages:
            return items
        page += 1


def parse_item(session, item):
    url = item.get('link') or ''
    if not url:
        return None
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    facts = [clean_text(node) for node in soup.select('span.fis-h6')]
    if len(facts) < 2:
        return None

    event_date, event_time = parse_datetime(facts[0], edition_year(item))
    venue, city = parse_location(facts[1])
    title = clean_text((item.get('title') or {}).get('rendered'))
    if not title or not event_date or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': clean_text((item.get('content') or {}).get('rendered')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    # Establishing a normal site session avoids the host's direct-API WAF rule.
    session.get(SOURCE_URL, timeout=45).raise_for_status()
    items = api_items(session)
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(parse_item, session, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape programme detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FestivalsantanderComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalsantander_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FestivalsantanderComCrawler().run()


if __name__ == '__main__':
    main()
