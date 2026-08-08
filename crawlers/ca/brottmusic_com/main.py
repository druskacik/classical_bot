from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://brottmusic.com/'
SOURCE = 'Brott Music Festival'
TICKETS_URL = 'https://tickets.brottmusic.com'
PERFORMANCES_API = f'{TICKETS_URL}/include/widgets/events/performancelist.asp'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Referer': f'{TICKETS_URL}/',
    'X-Requested-With': 'XMLHttpRequest',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%A, %B %d, %Y %I:%M:%S %p')
    except ValueError:
        return None


def performance_url(performance):
    event_id = performance.get('EventID')
    if not event_id:
        return ''
    return f'{TICKETS_URL}/eventperformances.asp?evt={event_id}'


def make_record(performance):
    title = clean_text(performance.get('PerformanceName') or performance.get('Event'))
    starts_at = parse_datetime(performance.get('PerformanceDateTime'))
    venue = clean_text(performance.get('Venue'))
    city = clean_text(performance.get('VenueCity'))
    country_code = clean_text(performance.get('VenueCountry')).upper()
    url = performance_url(performance)

    if (
        not title
        or starts_at is None
        or not venue
        or not city
        or len(country_code) != 2
        or not url
    ):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(performance.get('Description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        params = {
            'fromDate': '',
            'toDate': '',
            'venue': 0,
            'city': '',
            'swEvent': 0,
            'category': 0,
            'searchString': '',
            'searchType': 0,
            'showHidden': 1,
            'showPackages': 0,
            'action': 'perf',
            'listPageSize': 100,
            'listMaxSize': 0,
            'page': page,
            'cp': 0,
        }
        try:
            response = session.get(PERFORMANCES_API, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch performance list',
                event='crawler_page_failed',
                level='error',
                url=PERFORMANCES_API,
                page=page,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        performances = payload.get('performance') or []
        for performance in performances:
            record = make_record(performance)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped performance with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=performance_url(performance),
                )

        settings = payload.get('settings') or {}
        if not performances or settings.get('endOfList', True):
            break
        page += 1

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class BrottmusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brottmusic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
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
        return get_concerts()


def main():
    BrottmusicComCrawler().run()


if __name__ == '__main__':
    main()
