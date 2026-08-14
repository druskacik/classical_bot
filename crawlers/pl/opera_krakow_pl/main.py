import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera.krakow.pl/'
SOURCE = 'Opera Krakowska'
ARCHIVE_START = date(2020, 10, 1)
DEFAULT_CITY = 'Kraków'

HEADERS = {
    'User-Agent': 'Googlebot',
    'Accept': 'application/json, text/html;q=0.9',
    'Accept-Language': 'pl-PL,pl;q=0.9',
}

HOME_VENUES = {
    'Duża Scena',
    'Scena Kameralna',
    'Antresola',
    'Budynek Opery Krakowskiej',
}

PLACE_RULES = [
    ('Ogród Botaniczny Uniwersytetu Jagiellońskiego', 'Kraków', 'PL'),
    ('TVP Kraków Studio S-3 Łęg', 'Kraków', 'PL'),
    ('Dziedziniec Arkadowy Zamku Królewskiego na Wawelu', 'Kraków', 'PL'),
    ('Kopalnia Soli „Wieliczka”', 'Wieliczka', 'PL'),
    ('Muzeum Fotografii w Krakowie', 'Kraków', 'PL'),
    ('Filharmonia im. Karola Szymanowskiego w Krakowie', 'Kraków', 'PL'),
    ('Małopolskie Centrum Kultury SOKÓŁ w Nowym Sączu', 'Nowy Sącz', 'PL'),
    ('Centrum Sztuki Mościce w Tarnowie', 'Tarnów', 'PL'),
    ('Auditorium Maximum Uniwersytetu Jagiellońskiego', 'Kraków', 'PL'),
    ('Centrum Kongresowe ICE Kraków', 'Kraków', 'PL'),
    ('Zamek w Nowym Wiśniczu', 'Nowy Wiśnicz', 'PL'),
    ('Dziedziniec zewnętrzny Zamku w Wiśniczu', 'Nowy Wiśnicz', 'PL'),
    ('Zamek Królewski w Niepołomicach', 'Niepołomice', 'PL'),
    ('Opera Narodowa w Bukareszcie', 'Bukareszt', 'RO'),
    ('Teatr Antyczny Plovdiv', 'Płowdiw', 'BG'),
    ('State Opera Plovdiv', 'Płowdiw', 'BG'),
    ('Stadnina Koni Huculskich „Gładyszów”', 'Uście Gorlickie', 'PL'),
    ('Sala widowiskowa BSCK', 'Busko-Zdrój', 'PL'),
    ('Opera Bałtycka w Gdańsku', 'Gdańsk', 'PL'),
    ('Miejskie Centrum Kultury w Nowym Targu', 'Nowy Targ', 'PL'),
    ('Pijalnia', 'Krynica-Zdrój', 'PL'),
    ('Dom Kultury w Wolbromiu', 'Wolbrom', 'PL'),
    ('Hala Urania w Olsztynie', 'Olsztyn', 'PL'),
    ('Sanocki Dom Kultury', 'Sanok', 'PL'),
    ('Dom Kultury Polskiej w Wilnie', 'Wilno', 'LT'),
    ('Klasztor na Skałce', 'Kraków', 'PL'),
]


def month_range(start, end):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, accept='application/json'):
    last_error = None
    for _ in range(3):
        try:
            response = session.get(url, headers={'Accept': accept}, timeout=45)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
    raise last_error


def decode_api_response(text):
    # Cloudflare occasionally surrounds valid JSON with a reload script.
    start = text.find('{')
    if start < 0:
        raise ValueError('API response did not contain JSON')
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


def get_month(session, endpoint, year, month):
    url = urljoin(SOURCE_URL, f'ajax/{endpoint}?year={year}&month={month}')
    try:
        return decode_api_response(get_response(session, url)).get('performances') or []
    except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
        log_message(
            'Failed to scrape repertoire month', event='crawler_page_failed', level='warning',
            url=url, error_type=type(error).__name__, error_message=str(error),
        )
        return []


def location_from_place(place):
    place = clean_text(place)
    if not place or place.upper() == 'ON-LINE':
        return None
    if place in HOME_VENUES:
        return place, DEFAULT_CITY, 'PL'
    for marker, city, country_code in PLACE_RULES:
        if marker.casefold() in place.casefold():
            return marker, city, country_code
    return None


def parse_description(html, fallback_parts):
    soup = BeautifulSoup(html, 'html.parser')
    description = ''
    for title in soup.select('.detail-text__title'):
        if clean_text(title).casefold() == 'opis':
            container = title.find_next_sibling(class_='detail-text__content')
            description = clean_text(container)
            break
    parts = [clean_text(part) for part in fallback_parts if clean_text(part)]
    if description:
        parts.append(description)
    return '\n\n'.join(dict.fromkeys(parts)) or None


def parse_api_event(group):
    occurrence = group.get('0') or group.get(0)
    if not occurrence or occurrence.get('isOnlineEvent'):
        return None
    location = location_from_place(group.get('place'))
    if not location:
        return None
    title = clean_text(group.get('title'))
    slug = clean_text(group.get('slug'))
    date_value = ((occurrence.get('date') or {}).get('date') or '')[:10]
    time_value = ((occurrence.get('time') or {}).get('date') or '')[11:16]
    try:
        date.fromisoformat(date_value)
    except ValueError:
        return None
    if not title or not slug or not re.fullmatch(r'\d{2}:\d{2}', time_value):
        return None
    venue, city, country_code = location
    return {
        'occurrence_id': occurrence.get('id'),
        'title': title,
        'date': date_value,
        'url': urljoin(SOURCE_URL, f'spektakle/{slug}'),
        'time_from': time_value,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'fallback_description': [group.get('type'), group.get('composer'), group.get('postscript')],
    }


def get_concerts(today=None):
    today = today or date.today()
    future_end = date(today.year + 1, today.month, 1)
    session = requests.Session()
    session.headers.update(HEADERS)

    queries = []
    for year, month in month_range(ARCHIVE_START, date(today.year, today.month, 1)):
        queries.append(('repertuar-archiwum', year, month))
    for year, month in month_range(date(today.year, today.month, 1), future_end):
        queries.append(('repertuar', year, month))

    groups = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(get_month, session, *query) for query in queries]
        for future in as_completed(futures):
            groups.extend(future.result())

    events = {}
    for group in groups:
        event = parse_api_event(group)
        if event:
            key = event['occurrence_id'] or (
                event['url'], event['date'], event['time_from'], event['venue']
            )
            events[key] = event

    descriptions = {}
    detail_urls = {event['url'] for event in events.values()}

    def load_detail(url):
        try:
            return url, get_response(session, url, accept='text/html')
        except requests.RequestException as error:
            log_message(
                'Failed to scrape event detail', event='crawler_page_failed', level='warning',
                url=url, error_type=type(error).__name__, error_message=str(error),
            )
            return url, ''

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(load_detail, url) for url in detail_urls]
        for future in as_completed(futures):
            url, html = future.result()
            descriptions[url] = html

    records = []
    for event in events.values():
        event['description'] = parse_description(
            descriptions.get(event['url'], ''), event.pop('fallback_description')
        )
        event.pop('occurrence_id', None)
        records.append(event)
    return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title'], row['url']))


class OperaKrakowPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_krakow_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaKrakowPlCrawler().run()


if __name__ == '__main__':
    main()
