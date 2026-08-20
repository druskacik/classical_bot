import html
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'http://bartvanoort.nl/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'Bart-van-Oort/en-GB/calendar/archive.aspx')
SOURCE = 'Bart van Oort'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,nl;q=0.8',
}

COUNTRY_MARKERS = {
    'australia': 'AU', 'australië': 'AU', 'australie': 'AU',
    'austria': 'AT', 'oostenrijk': 'AT',
    'belgium': 'BE', 'belgië': 'BE', 'belgie': 'BE',
    'france': 'FR', 'frankrijk': 'FR',
    'italy': 'IT', 'italië': 'IT', 'italie': 'IT',
    'new zealand': 'NZ', 'norway': 'NO', 'polen': 'PL',
    'spain': 'ES', 'switzerland': 'CH', 'ukraine': 'UA',
    'engeland': 'GB', ' uk': 'GB',
    ' usa': 'US', 'new york': 'US', ' pa': 'US', ' ne': 'US',
}
COUNTRY_ABBREVIATIONS = {
    'b': 'BE', 'fr': 'FR', 'it': 'IT', 'sw': 'CH', 'usa': 'US',
}
CITY_COUNTRIES = {
    'adelaide': 'AU', 'alessandria': 'IT', 'bergen': 'NO',
    'bellinzona': 'CH', 'berowra heights': 'AU', 'bodio lomnago': 'IT',
    'brisbane': 'AU', 'brugge': 'BE', 'dreux': 'FR', 'genoa': 'IT',
    'greensboro': 'US', 'ithaca': 'US', 'kiev': 'UA', 'kremsmünster': 'AT',
    'lewisburg': 'US', 'lugano': 'CH', 'madrid': 'ES', 'narvik': 'NO',
    'oxborough': 'GB', 'paris': 'FR', 'perth': 'AU', 'perugia': 'IT',
    'piacenza': 'IT', 'ruiselede': 'BE', 'salzburg': 'AT', 'stavanger': 'NO',
    'sydney': 'AU', 'wellington': 'NZ', 'zakopane': 'PL',
}
VENUE_WORDS = re.compile(
    r'(?:academy|academie|auditorium|castle|chapel|church|codarts|concerten|'
    r'concertgebouw|concert hall|conservat|festival|gallery|hall|institut|kapel|'
    r'kerk|klooster|monastery|mozarteum|museum|muziekgebouw|muziekcentrum|paleis|'
    r'palazzo|pianos?|raadhuis|salon|schloss|school|theater|theatre|universit|'
    r'veeniging|veem|veste|villa|vredenburg|zaal)', re.I
)
CITY_FIRST = {
    'amsterdam', 'breda', 'brugge', 'den haag', 'dreux', 'hellendoorn',
    'kiev', 'kimswerd', 'lewisburg', 'madrid', 'middelburg', 'paris',
    'perugia', 'piacenza', 'utrecht', 'venegono',
}
KNOWN_VENUES = {
    'felix meritis': 'Amsterdam',
}


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n *', '\n', text).strip(' ,.-')


def parse_datetime(value):
    match = re.fullmatch(
        r'(\d{1,2}) ([A-Za-z]+) (\d{4})(?: (\d{1,2}):(\d{2}))?',
        clean_text(value),
    )
    if not match:
        return None, None
    try:
        parsed = datetime.strptime(' '.join(match.group(1, 2, 3)), '%d %B %Y')
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        time_from = f'{int(match.group(4)):02d}:{match.group(5)}'
    return parsed.date().isoformat(), time_from


def country_for(location, city):
    normalized = f' {location.lower()} '
    for marker, code in COUNTRY_MARKERS.items():
        if marker in normalized:
            return code
    for abbreviation, code in COUNTRY_ABBREVIATIONS.items():
        if re.search(rf'\({re.escape(abbreviation)}\)|,\s*{re.escape(abbreviation)}\b', normalized):
            return code
    return CITY_COUNTRIES.get(city.lower(), 'NL')


def parse_location(value):
    location = clean_text(value)
    if not location or re.search(
        r'\b(?:various|tour|on the air|private function)\b', location, re.I
    ):
        return None

    countryless = re.sub(
        r'\s*\((?:USA|B|Fr|It|Sw|Polen)\)\s*', '', location, flags=re.I
    )
    countryless = re.sub(
        r'[, .]*(?:Australia|Australië|Australie|Austria|Oostenrijk|Belgium|België|'
        r'Belgie|Engeland|France|Frankrijk|Italy|Italië|Italie|New Zealand|Norway|'
        r'Polen|Spain|Ukraine|UK|USA)\s*$', '', countryless, flags=re.I
    ).strip(' ,.-')

    known_city = KNOWN_VENUES.get(countryless.lower())
    if known_city:
        return countryless, known_city, country_for(location, known_city)

    parts = [part.strip() for part in countryless.split(',') if part.strip()]
    if len(parts) < 2:
        return None
    if not any(VENUE_WORDS.search(part) for part in parts):
        return None
    if parts[0].lower() in CITY_FIRST or (not VENUE_WORDS.search(parts[0]) and VENUE_WORDS.search(parts[1])):
        city, venue = parts[0], ', '.join(parts[1:])
    else:
        venue, city = ', '.join(parts[:-1]), parts[-1]
    city = re.sub(r'\s*\([^)]*\)\s*$', '', city).strip()
    if not city or not venue or city.lower() == venue.lower():
        return None
    return venue, city, country_for(location, city)


def parse_page(page_html, page_url):
    soup = BeautifulSoup(page_html, 'html.parser')
    records = []
    for item in soup.select('.ContentItemAppointment'):
        title = clean_text(item.select_one('.FormFieldTitle'))
        event_date, time_from = parse_datetime(item.select_one('.FormFieldDateTime'))
        location = parse_location(item.select_one('.FormFieldLocationString'))
        item_id = item.get('id', '')
        if not title or not event_date or not location or not item_id:
            continue
        venue, city, country_code = location
        description = clean_text(item.select_one('.AppointmentBody')) or None
        records.append({
            'title': title,
            'date': event_date,
            'url': f'{page_url}#{item_id}',
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class BartVanOortNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bartvanoort_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        session = requests.Session()
        session.headers.update(HEADERS)
        page_number = 1
        while True:
            page_url = f'{ARCHIVE_URL}?page={page_number}'
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            page_records = parse_page(response.text, page_url)
            records.extend(page_records)

            soup = BeautifulSoup(response.text, 'html.parser')
            next_link = soup.select_one('.CmsPager a.Next')
            if not next_link:
                break
            page_number += 1
            if page_number > 100:
                log_message(
                    'Stopped Bart van Oort archive at pagination safety limit',
                    event='crawler_pagination_limit',
                    level='warning',
                    url=page_url,
                    record_count=len(records),
                )
                break
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    BartVanOortNlCrawler().run()


if __name__ == '__main__':
    main()
