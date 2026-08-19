import calendar
import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.portlandopera.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar-of-events/')
EVENTS_API_URL = urljoin(SOURCE_URL, 'umbraco/surface/events/getevents')
SOURCE = 'Portland Opera'

# The calendar API accepts a month and returns historical as well as future events.
# 2020 comfortably predates the oldest calendar pages currently linked by the site.
FIRST_ARCHIVE_YEAR = 2020
FUTURE_MONTHS = 24

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Content-Type': 'application/json',
    'Referer': CALENDAR_URL,
    'X-Requested-With': 'XMLHttpRequest',
}

DATE_RE = re.compile(r'/Date\((\d+)\)/')
ADDRESS_START_RE = re.compile(r'^\d+[A-Za-z-]*\s')
CITY_STATE_RE = re.compile(
    r',\s*([A-Za-z][A-Za-z .\'-]+),\s*(?:OR|Oregon)\b', re.IGNORECASE
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def month_starts(start, end):
    current = start.replace(day=1)
    final = end.replace(day=1)
    while current <= final:
        yield current
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)


def future_limit(today):
    month_index = today.year * 12 + today.month - 1 + FUTURE_MONTHS
    year, zero_month = divmod(month_index, 12)
    return date(year, zero_month + 1, calendar.monthrange(year, zero_month + 1)[1])


def parse_start_date(value):
    match = DATE_RE.fullmatch(value or '')
    if not match:
        return None
    try:
        return datetime.fromtimestamp(int(match.group(1)) / 1000).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def infer_city(venue_name):
    venue_name = clean_text(venue_name)
    match = CITY_STATE_RE.search(venue_name)
    if match:
        return clean_text(match.group(1))

    # Named Portland Opera theatres in the production feed are all in Portland.
    if venue_name and not ADDRESS_START_RE.match(venue_name) and venue_name.lower() != 'various locations':
        return 'Portland'
    return None


def infer_venue(item, venue_name):
    venue_name = clean_text(venue_name)
    if not venue_name or venue_name.lower() == 'various locations':
        return None
    looks_like_address = bool(
        ADDRESS_START_RE.match(venue_name)
        or (re.search(r'\d', venue_name) and CITY_STATE_RE.search(venue_name))
    )
    if not looks_like_address:
        return venue_name

    # Community listings sometimes put only an address in Venue.Name. Their
    # first-party titles consistently identify the host after " at ".
    title = clean_text(item.get('Title'))
    parts = re.split(r'\s+at\s+', title, maxsplit=1, flags=re.IGNORECASE)
    return clean_text(parts[1]) if len(parts) == 2 else None


def record_from_item(item):
    title = clean_text(item.get('Title'))
    event_date = parse_start_date(item.get('StartDate'))
    relative_url = item.get('Url') or item.get('UmbUrl')
    url = urljoin(SOURCE_URL, relative_url or '')
    venue_data = item.get('Venue') or {}
    venue_name = clean_text(venue_data.get('Name'))
    venue = infer_venue(item, venue_name)
    city = infer_city(venue_name)

    if not all((title, event_date, relative_url, venue, city)):
        return None

    description_parts = [
        clean_text(item.get('SubtitleMarkdown') or item.get('Subtitle')),
        clean_text(item.get('Intro')),
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(item.get('TimeStr')),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_month(session, month):
    records = []
    page = 0
    while True:
        response = session.post(
            EVENTS_API_URL,
            json={
                'date': month.isoformat(),
                'category': 0,
                'page': page,
                'sort': 2,
                'sortDir': 0,
            },
            timeout=45,
            verify=False,
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get('Items') or []:
            record = record_from_item(item)
            if record:
                records.append(record)

        pages = int(payload.get('Pages') or 0)
        if page >= pages:
            break
        page += 1
    return records


def scrape_concerts(session=None, start=None, end=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    today = date.today()
    start = start or date(FIRST_ARCHIVE_YEAR, 1, 1)
    end = end or future_limit(today)
    records = []
    for month in month_starts(start, end):
        try:
            records.extend(fetch_month(session, month))
        except (requests.RequestException, ValueError, TypeError) as error:
            log_message(
                'Calendar month could not be fetched',
                event='crawler_month_failed',
                level='warning',
                url=EVENTS_API_URL,
                month=month.isoformat(),
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda row: (row['date'], row['time_from'] or '', row['title']))
    if not result:
        log_message(
            'No parseable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return result


class PortlandOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='portlandopera_org',
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
    PortlandOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
