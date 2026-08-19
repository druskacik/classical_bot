import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://koa.gr/'
SOURCE = 'Athens State Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'el-GR,el;q=0.9,en;q=0.7',
}

MONTHS = {
    'ιανουαρίου': 1, 'φεβρουαρίου': 2, 'μαρτίου': 3, 'απριλίου': 4,
    'μαΐου': 5, 'μαϊου': 5, 'ιουνίου': 6, 'ιουλίου': 7, 'αυγούστου': 8,
    'σεπτεμβρίου': 9, 'οκτωβρίου': 10, 'νοεμβρίου': 11, 'δεκεμβρίου': 12,
}

# The orchestra is Athens-based, but its archive also contains tours. These
# markers prevent the home-city default from being applied to touring events.
CITY_MARKERS = {
    'αθήνα': ('Athens', 'GR'), 'αθηνών': ('Athens', 'GR'), 'athens': ('Athens', 'GR'),
    'θεσσαλονίκη': ('Thessaloniki', 'GR'), 'thessaloniki': ('Thessaloniki', 'GR'),
    'τρίκαλα': ('Trikala', 'GR'), 'τρικάλων': ('Trikala', 'GR'), 'trikala': ('Trikala', 'GR'),
    'πάτρα': ('Patras', 'GR'), 'πατρών': ('Patras', 'GR'), 'patras': ('Patras', 'GR'),
    'καλαμάτα': ('Kalamata', 'GR'), 'kalamata': ('Kalamata', 'GR'),
    'ναύπλιο': ('Nafplio', 'GR'), 'nafplio': ('Nafplio', 'GR'),
    'ιωάννινα': ('Ioannina', 'GR'), 'ioannina': ('Ioannina', 'GR'),
    'βόλος': ('Volos', 'GR'), 'volos': ('Volos', 'GR'),
    'λαμία': ('Lamia', 'GR'), 'lamia': ('Lamia', 'GR'),
    'κέρκυρα': ('Corfu', 'GR'), 'corfu': ('Corfu', 'GR'),
    'ηράκλειο': ('Heraklion', 'GR'), 'heraklion': ('Heraklion', 'GR'),
    'χίος': ('Chios', 'GR'), 'chios': ('Chios', 'GR'),
    'σύρος': ('Syros', 'GR'), 'syros': ('Syros', 'GR'),
    'δελφοί': ('Delphi', 'GR'), 'delphi': ('Delphi', 'GR'),
    'ελευσίνα': ('Elefsina', 'GR'), 'elefsina': ('Elefsina', 'GR'),
    'άμστερνταμ': ('Amsterdam', 'NL'), 'amsterdam': ('Amsterdam', 'NL'),
    'terneuzen': ('Terneuzen', 'NL'), 'τερνέζεν': ('Terneuzen', 'NL'),
}
TOUR_MARKERS = ('εκτός έδρας', 'περιοδεία', 'on tour', 'concertgebouw', 'festival terneuzen')


def clean_text(value, separator='\n'):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    text = clean_text(value, ' ').casefold().replace(',', ' ')
    match = re.search(r'(\d{1,2})\s+([α-ωάέήίόύώϊΐϋΰ]+)\s+(\d{4})', text)
    if not match:
        return None, None
    month = MONTHS.get(match.group(2))
    if not month:
        return None, None
    try:
        event_date = datetime(int(match.group(3)), month, int(match.group(1))).date().isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, time_from


def geography_for(title, venue):
    evidence = f'{title} {venue}'.casefold()
    for marker, geography in CITY_MARKERS.items():
        if marker.casefold() in evidence:
            return geography
    if any(marker in evidence for marker in TOUR_MARKERS):
        return '', ''
    return 'Athens', 'GR'


def parse_event(payload, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    title_node = soup.select_one('h1.event_title')
    date_node = soup.select_one('article h3.imerominia')
    venue_node = soup.select_one('article h4.hall')
    title = clean_text(title_node, ' ') or clean_text(payload.get('title', {}).get('rendered'), ' ')
    venue = clean_text(venue_node, ' ')
    event_date, time_from = parse_date_time(date_node)
    city, country_code = geography_for(title, venue)
    if not title or not event_date or not venue or not city:
        return None

    description_parts = [clean_text(payload.get('content', {}).get('rendered'))]
    programme_heading = soup.select_one('.programa-ekdilosis')
    if programme_heading:
        container = programme_heading.parent
        description_parts.append(clean_text(container))
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None
    return {
        'title': title,
        'date': event_date,
        'url': payload['link'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_event_payloads(session):
    records = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        # Polylang does not honour its `lang` parameter on this custom REST
        # route, so discard English translations and retain canonical Greek
        # event pages only.
        records.extend(item for item in batch if '/en/events/' not in item.get('link', ''))
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return records


def fetch_and_parse(payload):
    response = requests.get(payload['link'], headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(payload, response.text)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    payloads = get_event_payloads(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_and_parse, payload): payload['link'] for payload in payloads}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Athens State Orchestra event',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class KoaGrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='koa_gr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GR',
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
    KoaGrCrawler().run()


if __name__ == '__main__':
    main()
