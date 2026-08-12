import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gabrieli.com/'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
SOURCE = 'Gabrieli Consort & Players'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(\d{1,2})(?:st|nd|rd|th)?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})[.:](\d{2})\s*(am|pm)?\b', re.IGNORECASE)

# The calendar covers Gabrieli's concerts and tours.  These explicit mappings
# prevent its London office address from being applied to touring performances.
INTERNATIONAL_CITIES = {
    'Amsterdam': 'NL', 'Antwerp': 'BE', 'Barcelona': 'ES', 'Berlin': 'DE',
    'Brussels': 'BE', 'Cologne': 'DE', 'Dublin': 'IE', 'Eindhoven': 'NL',
    'Ghent': 'BE', 'Göttingen': 'DE', 'Halle': 'DE', 'Hamburg': 'DE',
    'Kraków': 'PL', 'Leipzig': 'DE', 'Limerick': 'IE', 'Lisbon': 'PT',
    'Madrid': 'ES', 'Milan': 'IT', 'Munich': 'DE', 'Paris': 'FR',
    'Poznań': 'PL', 'Prague': 'CZ', 'Rotterdam': 'NL', 'Salzburg': 'AT',
    'Seville': 'ES', 'Siena': 'IT', 'Utrecht': 'NL', 'Vienna': 'AT',
    'Warsaw': 'PL', 'Wrocław': 'PL', 'Wroclaw': 'PL', 'Zurich': 'CH',
}
UK_CITIES = {
    'Bath', 'Birmingham', 'Bristol', 'Cambridge', 'Canterbury', 'Cardiff',
    'Chichester', 'Derby', 'Durham', 'Edinburgh', 'Ely', 'Exeter', 'Glasgow',
    'Guildford', 'Leeds', 'Liverpool', 'London', 'Manchester', 'Norwich',
    'Nottingham', 'Oxford', 'Salisbury', 'Sheffield', 'Winchester', 'York',
}
VENUE_CITIES = {
    'Barbican': ('London', 'GB'),
    'Cadogan Hall': ('London', 'GB'),
    'Ely Cathedral': ('Ely', 'GB'),
    'King’s College Chapel': ('Cambridge', 'GB'),
    "King's College Chapel": ('Cambridge', 'GB'),
    'Milton Court': ('London', 'GB'),
    'Royal Albert Hall': ('London', 'GB'),
    "St John's Smith Square": ('London', 'GB'),
    'TivoliVredenburg': ('Utrecht', 'NL'),
    'Westminster Abbey': ('London', 'GB'),
}
VENUE_WORDS = re.compile(
    r'\b(?:abbey|basilica|cathedral|chapel|church|concertgebouw|hall|minter|minster|'
    r'museum|opera|palace|theatre|forum|college|school|arts centre)\b', re.IGNORECASE
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    suffix = (match.group(3) or '').lower()
    if minute > 59 or hour > (12 if suffix else 23):
        return None
    if suffix == 'pm' and hour != 12:
        hour += 12
    elif suffix == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def country_for_city(city):
    if city in INTERNATIONAL_CITIES:
        return INTERNATIONAL_CITIES[city]
    if city in UK_CITIES:
        return 'GB'
    return None


def parse_location(value):
    location = clean_text(value).strip(' ,')
    if not location:
        return None

    for venue_name, (city, country) in VENUE_CITIES.items():
        if venue_name.casefold() in location.casefold():
            venue = location if VENUE_WORDS.search(location) else venue_name
            return venue, city, country

    parts = [part.strip() for part in location.split(',') if part.strip()]
    if len(parts) >= 2:
        city = parts[-1]
        country = country_for_city(city)
        if country:
            venue = ', '.join(parts[:-1])
            return venue, city, country

    # Names such as "Ely Cathedral" and "Durham Cathedral" carry both fields.
    if VENUE_WORDS.search(location):
        for city in sorted(UK_CITIES | set(INTERNATIONAL_CITIES), key=len, reverse=True):
            if re.search(rf'\b{re.escape(city)}\b', location, re.IGNORECASE):
                return location, city, country_for_city(city)
    return None


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return list(dict.fromkeys(clean_text(node) for node in soup.select('url > loc')))


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    event = soup.select_one('#main .event-page')
    if not event:
        return None
    title = clean_text(event.select_one('h2'))
    date_text = clean_text(event.select_one('.date'))
    event_date = parse_date(date_text)
    location = parse_location(event.select_one('h3'))
    if not title or not event_date or not location:
        return None
    venue, city, country_code = location
    description = clean_text(event.select_one('.copy')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_text),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


class GabrieliComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gabrieli_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = event_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(get_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_event(response.content, url)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Gabrieli event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    GabrieliComCrawler().run()


if __name__ == '__main__':
    main()
