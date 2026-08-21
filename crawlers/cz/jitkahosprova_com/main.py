import re
import unicodedata
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'http://jitkahosprova.com/'
SOURCE = 'Jitka Hosprová'
CALENDAR_URLS = (
    urljoin(SOURCE_URL, 'concerts/'),
    urljoin(SOURCE_URL, 'concerts-archive/'),
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
}

NON_EVENTS = re.compile(
    r'\b(?:PRIVAT(?:E)? EVENT|MASTERCLASS|MASTERLASS|POROTCE|SOUTĚŽ KONZERVATOŘÍ)\b',
    re.IGNORECASE,
)
DATE_RANGE = re.compile(r'\d{1,2}\s*[.\-/]?\s*(?:–|—|-)\s*\d{1,2}')
TIME = re.compile(r'\b([01]?\d|2[0-3]):([0-5]\d)\b')
PRICE = re.compile(r',?\s*\d[\d\s]*(?:–|—|-)\s*\d[\d\s]*\s*KČ.*$', re.IGNORECASE)

COUNTRY_MARKERS = {
    'CH': 'CH',
    'SWITZERLAND': 'CH',
    'ITA': 'IT',
    'ITALY': 'IT',
    'POL': 'PL',
    'POLAND': 'PL',
    'DE': 'DE',
    'GERMANY': 'DE',
    'CZ': 'CZ',
    'CZECH REPUBLIC': 'CZ',
}

CITY_COUNTRIES = {
    'ALBA': 'IT',
    'BERLIN': 'DE',
    'BOLOGNA': 'IT',
    'BURGDORF': 'CH',
    'GARFAGNANA': 'IT',
    'RIMINI': 'IT',
    'RIMINY': 'IT',
    'SZCZECIN': 'PL',
    'WIEN': 'AT',
    'VIENNA': 'AT',
}


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip(' \t\r\n–—-,')


def ascii_upper(value):
    normalized = unicodedata.normalize('NFKD', value)
    return ''.join(char for char in normalized if not unicodedata.combining(char)).upper()


def parse_date(value):
    value = clean_text(value).replace('\xa0', ' ')
    if DATE_RANGE.search(value):
        return None
    for date_format in ('%d.%m. %Y', '%d.%m.%Y', '%d %b %Y', '%d %B %Y'):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def country_from_location(location, city):
    normalized = ascii_upper(location)
    for marker, country_code in COUNTRY_MARKERS.items():
        if re.search(rf'(?<![A-Z]){re.escape(marker)}(?![A-Z])', normalized):
            return country_code
    return CITY_COUNTRIES.get(ascii_upper(city), 'CZ')


def strip_country_markers(value):
    result = value
    for marker in COUNTRY_MARKERS:
        result = re.sub(
            rf'\s*[/,]\s*{re.escape(marker)}\b', '', result, flags=re.IGNORECASE
        )
    return clean_text(result)


def parse_location(value):
    location = clean_text(TIME.sub('', value))
    location = PRICE.sub('', location).strip()
    location = re.sub(r'^\s*A\s+', '', location, flags=re.IGNORECASE)

    # Calendar entries consistently put the city before a dash and the venue
    # after it. Additional address-like text following another dash is ignored.
    parts = [clean_text(part) for part in re.split(r'\s+(?:–|—|-)\s+', location) if clean_text(part)]
    if len(parts) >= 2:
        city = strip_country_markers(parts[0])
        venue = parts[1]
    elif ',' in location:
        city, venue = [clean_text(part) for part in location.split(',', 1)]
        # Legacy entries sometimes contain only "country, city" or
        # "city, country". Neither half is a defensible venue.
        if ascii_upper(city) in COUNTRY_MARKERS or ascii_upper(venue) in COUNTRY_MARKERS:
            return None
        city = strip_country_markers(city)
    else:
        normalized = ascii_upper(location)
        city_name = next(
            (name for name in CITY_COUNTRIES if normalized.startswith(f'{name} ')),
            None,
        )
        if not city_name:
            return None
        words = location.split(maxsplit=1)
        city, venue = words[0], words[1]

    city = re.sub(r'^\(.*?\)\s*', '', city).strip()
    venue = strip_country_markers(venue)
    if not city or not venue or ascii_upper(city) == ascii_upper(venue):
        return None
    if re.match(r'^(?:UL\.?|VIA|STREET|NÁMĚSTÍ)\b', ascii_upper(venue)):
        return None
    return venue, city, country_from_location(location, city)


def event_url(container, page_url):
    link = container.select_one('a[href]')
    if not link:
        return page_url
    href = clean_text(link.get('href'))
    if not href or href.endswith('/#'):
        return page_url
    return urljoin(page_url, href)


def parse_event(container, page_url):
    date_heading = container.find('h3')
    headings = [clean_text(item.get_text(' ', strip=True)) for item in container.find_all('h2')]
    if not date_heading or len(headings) < 2:
        return None

    raw_date = clean_text(date_heading.get_text(' ', strip=True))
    title = headings[0]
    description = '\n'.join(headings)
    if not title or NON_EVENTS.search(description):
        return None
    event_date = parse_date(raw_date)
    if not event_date:
        return None

    location_line = headings[-1]
    location = parse_location(location_line)
    if not location:
        return None
    venue, city, country_code = location
    time_match = TIME.search(location_line)

    return {
        'title': title,
        'date': event_date,
        'url': event_url(container, page_url),
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    records = []
    for page_url in CALENDAR_URLS:
        log_message('Fetching concert calendar', event='crawler_url_fetch', url=page_url)
        response = requests.get(page_url, headers=HEADERS, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for container in soup.select('.concerts'):
            try:
                record = parse_event(container, page_url)
            except (AttributeError, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse calendar entry',
                    event='crawler_item_failed',
                    level='warning',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class JitkaHosprovaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jitkahosprova_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    JitkaHosprovaComCrawler().run()


if __name__ == '__main__':
    main()
