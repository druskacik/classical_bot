import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orlandophil.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
AJAX_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
SOURCE = 'Orlando Philharmonic'
CALENDAR_ID = '14885'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_FORMATS = (
    '%I:%M%p, %A, %B %d, %Y',
    '%I%p, %A, %B %d, %Y',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    value = re.sub(r'\s+', ' ', clean_text(value)).strip()
    for date_format in DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, date_format)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            pass
    return None


def calendar_event_urls(session):
    current_year = datetime.now(timezone.utc).year
    urls = set()
    empty_future_years = 0

    # The calendar currently retains no old occurrences, but future runs should
    # still collect any archive that the first-party endpoint makes available.
    for year in range(2010, current_year + 6):
        start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
        response = session.post(
            AJAX_URL,
            data={
                'action': 'vem_get_events',
                'id': CALENDAR_ID,
                'event': '0',
                'start': str(start),
                'end': str(end),
                'moment': str(int(datetime.now(timezone.utc).timestamp())),
                'futureOnly': 'false',
            },
            headers={'X-Requested-With': 'XMLHttpRequest'},
            timeout=45,
        )
        response.raise_for_status()
        events = response.json().get('events', [])

        for event in events:
            event_id = str(event.get('eventId') or '').strip()
            if event_id.isdigit():
                urls.add(f'{SOURCE_URL}?post_type=event&p={event_id}')

        if year >= current_year:
            empty_future_years = empty_future_years + 1 if not events else 0
            if empty_future_years >= 2:
                break

    return sorted(urls)


def detail_records(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get('href', '').strip() if canonical else response.url
    title = clean_text(soup.select_one('h1'))

    content = soup.select_one('.vem-single-event-content')
    description_parts = []
    if content:
        for selector in (
            '.vem-single-event-field-set',
            '.vem-single-event-details',
            '.vem-single-event-media',
        ):
            for node in content.select(selector):
                text = clean_text(node)
                if text and text not in description_parts:
                    description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for occurrence in soup.select('.vem-one-occurrence'):
        parsed = parse_datetime(occurrence.select_one('.vem-single-event-date-start'))
        venue = clean_text(occurrence.select_one('.vem-single-occurrence-venue'))
        city_value = clean_text(occurrence.select_one('.venue-city'))
        city = city_value.split(',', 1)[0].strip()
        if not title or not parsed or not venue or not city or not canonical_url:
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': canonical_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    urls = calendar_event_urls(session)
    for url in urls:
        try:
            records.extend(detail_records(session, url))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Event detail could not be parsed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No calendar occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class OrlandoPhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orlandophil_org',
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
    OrlandoPhilOrgCrawler().run()


if __name__ == '__main__':
    main()
