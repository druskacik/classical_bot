import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.agarita.org/'
SOURCE = 'Agarita'
CALENDAR_PATHS = ('2627', '2526', 'shows', 'past')
TIME_ZONE = ZoneInfo('America/Chicago')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def calendar_items(session, path):
    url = urljoin(SOURCE_URL, path)
    params = {'format': 'json', 'view': 'list'}
    seen_offsets = set()

    while True:
        response = session.get(url, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        yield from payload.get('upcoming', [])
        yield from payload.get('past', [])

        pagination = payload.get('pagination') or {}
        offset = pagination.get('nextPageOffset')
        if not pagination.get('nextPage') or offset is None or offset in seen_offsets:
            break
        seen_offsets.add(offset)
        params['offset'] = offset


def city_and_country(location):
    line_two = clean_text(location.get('addressLine2'))
    country = clean_text(location.get('addressCountry')).lower()
    line_one = clean_text(location.get('addressLine1'))
    venue = clean_text(location.get('addressTitle'))

    if country == 'spain' or 'la rioja' in line_one.lower():
        match = re.search(r'\b(?:\d{5}\s+)?([^,]+),\s*La Rioja\b', line_one, re.I)
        return (clean_text(match.group(1)) if match else 'Enciso'), 'ES'

    match = re.match(r'([^,]+),\s*[A-Z]{2}\b', line_two)
    if match:
        return clean_text(match.group(1)), 'US'

    match = re.fullmatch(r'([^,]+),\s*[A-Z]{2}', venue)
    if match:
        return clean_text(match.group(1)), 'US'

    # Agarita's calendars overwhelmingly describe performances in its home city.
    # Touring entries name their destination in the structured location fields
    # and are handled above.
    return 'San Antonio', 'US'


def item_to_record(item):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    start = item.get('startDate')
    full_url = item.get('fullUrl')
    if not title or not start or not full_url:
        return None

    if not venue and title == 'Humble Hall in District 1':
        venue = 'Beacon Hill Porchfest'
    if not venue:
        return None

    # A city/state string is useful as a location hint but is not a venue.
    if re.fullmatch(r'[^,]+,\s*[A-Z]{2}', venue):
        return None

    try:
        start_at = datetime.fromtimestamp(float(start) / 1000, tz=TIME_ZONE)
    except (TypeError, ValueError, OverflowError):
        return None

    city, country_code = city_and_country(location)
    if not city:
        return None

    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def library_occurrence_records(item):
    """Expand the two library performances stored in a single calendar body."""
    if clean_text(item.get('title')) != 'Agarita Inspires! at Libraries':
        return []
    body = clean_text(item.get('body'))
    pattern = re.compile(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
        r'[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s*'
        r'(\d{1,2}(?::\d{2})?\s*[ap]m)\s*\n?,?\s*([^,\n]*Library)',
        re.I,
    )
    base = item_to_record({
        **item,
        'location': {'addressTitle': 'temporary venue'},
    })
    if not base:
        return []

    records = []
    for time_text, venue in pattern.findall(body):
        try:
            parsed_time = datetime.strptime(time_text.replace(' ', '').upper(), '%I:%M%p')
        except ValueError:
            try:
                parsed_time = datetime.strptime(time_text.replace(' ', '').upper(), '%I%p')
            except ValueError:
                continue
        records.append({
            **base,
            'time_from': parsed_time.strftime('%H:%M'),
            'venue': clean_text(venue),
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records_by_id = {}

    for path in CALENDAR_PATHS:
        try:
            for item in calendar_items(session, path):
                record = item_to_record(item)
                if record:
                    records_by_id[str(item.get('id') or record['url'])] = record
                else:
                    for index, occurrence in enumerate(library_occurrence_records(item)):
                        records_by_id[f"{item.get('id')}:{index}"] = occurrence
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Agarita calendar request failed',
                event='crawler_calendar_failed',
                level='warning',
                url=urljoin(SOURCE_URL, path),
                error_type=type(error).__name__,
                error_message=str(error),
            )

    records = sorted(
        records_by_id.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )
    if not records:
        log_message(
            'No Agarita events found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return records


class AgaritaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='agarita_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    AgaritaOrgCrawler().run()


if __name__ == '__main__':
    main()
