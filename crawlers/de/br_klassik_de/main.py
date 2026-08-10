import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.br-klassik.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'programm/konzerte/index.html')
CALENDAR_API = urljoin(
    SOURCE_URL, 'programm/konzerte/konzerte-102~calendarItems.jsp'
)
SOURCE = 'BR-KLASSIK Konzerte'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The calendar belongs to a German broadcaster but includes its ensembles'
# international tours. Cities outside Germany need the event's actual country.
FOREIGN_CITY_COUNTRIES = {
    'Aix-en-Provence': 'FR',
    'Amsterdam': 'NL',
    'Barcelona': 'ES',
    'Birmingham': 'GB',
    'Brüssel': 'BE',
    'Budapest': 'HU',
    'Chicago': 'US',
    'Dublin': 'IE',
    'Dubrovnik': 'HR',
    'Grafenegg': 'AT',
    'Helsinki': 'FI',
    'Kaohsiung': 'TW',
    'Katowice': 'PL',
    'Kawasaki-City': 'JP',
    'Las Palmas de Gran Canaria': 'ES',
    'Liverpool': 'GB',
    'London': 'GB',
    'Luxembourg': 'LU',
    'Luxemburg': 'LU',
    'Luzern': 'CH',
    'Madrid': 'ES',
    'Mailand': 'IT',
    'Nagoya-shi': 'JP',
    'New York': 'US',
    'Nishinomiya': 'JP',
    'Paris': 'FR',
    'Philadelphia': 'US',
    'Prag': 'CZ',
    'Riga': 'LV',
    'Salzburg': 'AT',
    'Santa Cruz de Tenerife': 'ES',
    'Seoul': 'KR',
    'Taichung': 'TW',
    'Taipei': 'TW',
    'Tokio': 'JP',
    'València': 'ES',
    'Washington D.C.': 'US',
    'Wien': 'AT',
    'Wrocław': 'PL',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


def calendar_items(direction, cursor):
    """Read one side of the cursor-based calendar, including its archive."""
    items = []
    seen_datetimes = set()
    while cursor:
        payload = get_response(
            CALENDAR_API, {direction: cursor, 'rows': 100}
        ).json()
        if not payload:
            break
        items.extend(payload)
        next_cursor = payload[-1].get('datetime')
        if not next_cursor or next_cursor == cursor or next_cursor in seen_datetimes:
            break
        seen_datetimes.add(next_cursor)
        cursor = next_cursor
        if len(payload) < 100:
            break
    return items


def listing_items():
    now = datetime.now().replace(microsecond=0).isoformat()
    return calendar_items('to', now) + calendar_items('from', now)


def parse_location(value):
    text = clean_text(value)
    if not text:
        return None
    city, separator, venue = text.partition(',')
    city, venue = city.strip(), venue.strip()

    # A few records omit the city before the comma but repeat it in the
    # unambiguous venue name.
    if not city and venue == 'Turnierplatz Bad Kissingen':
        city = 'Bad Kissingen'
    elif not city and venue == 'St. Johannes Neumarkt':
        city = 'Neumarkt'

    if not separator or not city or not venue:
        return None
    return city, venue, FOREIGN_CITY_COUNTRIES.get(city, 'DE')


def parse_listing_item(item):
    soup = BeautifulSoup(item.get('html') or '', 'html.parser')
    title_node = soup.select_one('.br-title')
    location_node = soup.select_one('.br-content .br-text')
    link = soup.select_one('a[href*="/ausstrahlung-"][href$=".html"]')
    title = clean_text(title_node)
    location = parse_location(location_node)
    raw_datetime = item.get('datetime') or ''
    try:
        start = datetime.fromisoformat(raw_datetime)
    except ValueError:
        return None
    if not title or not location or not link:
        return None
    city, venue, country_code = location
    url = urljoin(SOURCE_URL, link.get('href'))
    fallback = clean_text(soup.select_one('.br-detail')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': fallback,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(url):
    soup = BeautifulSoup(get_response(url).text, 'html.parser')
    section = soup.select_one('section.br-calendar-detail')
    if not section:
        return None

    parts = []
    main_text = section.select_one('.br-main-text')
    if main_text:
        for node in main_text.find_all(['h3', 'p'], recursive=True):
            # Invalid nested paragraphs occur on the site; keep only leaf text
            # so the long description is not duplicated.
            if node.name == 'p' and node.find('p'):
                continue
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)

    for heading in section.find_all('h3'):
        label = clean_text(heading)
        if label.lower() not in {'programm', 'mitwirkende'}:
            continue
        table = heading.find_next('table')
        if table and table.find_previous('h3') == heading:
            table_text = clean_text(table)
            if table_text:
                parts.append(f'{label}\n{table_text}')
    return clean_text('\n\n'.join(parts)) or None


def get_concerts():
    records_by_url = {}
    for item in listing_items():
        record = parse_listing_item(item)
        if record:
            records_by_url[record['url']] = record

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(detail_description, url): record
            for url, record in records_by_url.items()
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result() or record['description']
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records_by_url.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class BrKlassikDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='br_klassik_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BrKlassikDeCrawler().run()


if __name__ == '__main__':
    main()
