from datetime import date
from urllib.parse import urljoin

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.metopera.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
EVENTS_API = urljoin(SOURCE_URL, 'ace-api/events/')
SOURCE = 'Metropolitan Opera'
CITY = 'New York'
VENUE = 'Metropolitan Opera House'

HEADERS = {
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def add_months(value, count):
    month_index = value.year * 12 + value.month - 1 + count
    return date(month_index // 12, month_index % 12 + 1, 1)


def month_ranges():
    """Yield the current month and the following 18 calendar months.

    The API rejects long date windows, while the public calendar requests one
    month at a time. Starting at the first of the current month also retains
    performances earlier in that month whenever the API still publishes them.
    """
    first = date.today().replace(day=1)
    for offset in range(19):
        start = add_months(first, offset)
        # The public endpoint treats this as an exclusive boundary. It also
        # intermittently returns 500 for some month-end dates, while the first
        # day of the following month is the stable boundary used here.
        end = add_months(start, 1)
        yield start.isoformat(), end.isoformat()


def clean_text(value):
    if not value:
        return ''
    return ' '.join(str(value).replace('\xa0', ' ').split())


def get_events(session, start_date, end_date):
    response = session.get(
        EVENTS_API,
        params={'startDate': start_date, 'endDate': end_date},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError('Met calendar API returned a non-list response')
    return payload


def event_description(event):
    parts = []
    composer = clean_text(event.get('composer'))
    if composer:
        parts.append(f'Composer: {composer}')
    synopsis = clean_text(event.get('synopsis'))
    if synopsis:
        parts.append(synopsis)
    artists = clean_text(event.get('artistCredits'))
    if artists:
        parts.append(f'Artists: {artists}')
    return '\n\n'.join(parts) or None


def make_record(event):
    # The calendar is mixed with recording-only cinema relays. "On Stage" is
    # the first-party category for concrete performances at the opera house.
    if 'On Stage' not in (event.get('categories') or []):
        return None
    if clean_text(event.get('location')).lower() != 'opera house':
        return None

    title = clean_text(event.get('name'))
    start = clean_text(event.get('eventDate'))
    detail_path = clean_text(event.get('viewDetailCtaUrl'))
    if not title or not detail_path or len(start) < 16:
        return None
    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except ValueError:
        return None

    time_from = start[11:16] if start[10:11] == 'T' else None
    if time_from and (len(time_from) != 5 or time_from[2] != ':'):
        time_from = None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, detail_path),
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': event_description(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records_by_occurrence = {}
    for start_date, end_date in month_ranges():
        try:
            events = get_events(session, start_date, end_date)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape calendar month',
                event='crawler_page_failed',
                level='warning',
                url=CALENDAR_URL,
                start_date=start_date,
                end_date=end_date,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for event in events:
            record = make_record(event)
            if record:
                key = (
                    record['title'], record['date'], record['time_from'], record['venue']
                )
                records_by_occurrence[key] = record

    return sorted(
        records_by_occurrence.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class MetoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='metopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MetoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
