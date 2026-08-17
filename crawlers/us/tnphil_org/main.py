from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tnphil.org/'
SOURCE = 'Tennessee Philharmonic Orchestra'
API_URL = 'https://tnphil.org/wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    return ' '.join(soup.get_text(' ', strip=True).split())


def parse_description(value):
    """Return authored event copy, excluding ticket, organizer, and venue blocks."""
    if not value:
        return None
    soup = BeautifulSoup(value, 'html.parser')
    paragraphs = soup.select('p.wp-block-paragraph')
    description = '\n\n'.join(
        text for paragraph in paragraphs if (text := clean_text(str(paragraph)))
    )
    return description or None


def parse_event(event):
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None

    title = clean_text(event.get('title'))
    url = event.get('url')
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    start_value = event.get('start_date')
    if not all((title, url, venue, city, start_value)):
        return None

    try:
        start = datetime.fromisoformat(start_value)
    except (TypeError, ValueError):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': parse_description(event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class TnphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tnphil_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []

        try:
            # The API rejects a single very broad date range. Ten-year windows
            # cover retained archives and future listings without that limit.
            final_year = date.today().year + 10
            for first_year in range(2000, final_year + 1, 10):
                last_year = min(first_year + 9, final_year)
                page = 1
                while True:
                    response = session.get(
                        API_URL,
                        params={
                            'page': page,
                            'per_page': 50,
                            'start_date': f'{first_year}-01-01 00:00:00',
                            'end_date': f'{last_year}-12-31 23:59:59',
                            'status': 'publish',
                        },
                        timeout=45,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    for event in payload.get('events', []):
                        record = parse_event(event)
                        if record:
                            records.append(record)

                    total_pages = int(payload.get('total_pages') or 1)
                    if page >= total_pages:
                        break
                    page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Tennessee Philharmonic Orchestra events',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    TnphilOrgCrawler().run()


if __name__ == '__main__':
    main()
