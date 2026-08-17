from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://salisburysymphony.org/'
SOURCE = 'Salisbury Symphony'
SHOWS_API = urljoin(SOURCE_URL, 'api/shows')
CITY = 'Salisbury'
COUNTRY_CODE = 'US'
STATUSES = ('PUBLISHED', 'ARCHIVED')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_html(value):
    if not value:
        return None
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return '\n'.join(lines) or None


def show_description(show):
    parts = []
    body = clean_html(show.get('content') or show.get('show_description'))
    if body:
        parts.append(body)
    program = []
    for row in show.get('program_rows') or []:
        if not isinstance(row, dict):
            continue
        values = [str(row.get(key) or '').strip() for key in ('left', 'center', 'right')]
        line = ' — '.join(value for value in values if value)
        if line:
            program.append(line)
    if program:
        parts.append('Program\n' + '\n'.join(program))
    return '\n\n'.join(parts) or None


def performance_datetime(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    # The first-party UI renders these values as wall-clock times without a
    # timezone conversion, despite the API's trailing Z. Match that behavior.
    return parsed


def show_records(show):
    title = str(show.get('title') or '').strip()
    slug = str(show.get('slug') or '').strip()
    venue = str(show.get('venue_name') or '').strip()
    venue_slug = str(show.get('venue_slug') or '').strip().lower()

    # These archived pandemic entries are explicitly online/at-home streams,
    # rather than public performances at a physical venue.
    if venue_slug == 'your-living-room':
        return []
    if not title or not slug or not venue:
        return []

    url = urljoin(SOURCE_URL, f'show/{slug}')
    description = show_description(show)
    records = []
    for value in show.get('showtimes') or []:
        performance = performance_datetime(value)
        if not performance:
            continue
        records.append({
            'title': title,
            'date': performance.date().isoformat(),
            'url': url,
            'time_from': performance.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []

    for status in STATUSES:
        try:
            response = session.get(
                SHOWS_API,
                params={'status': status, 'sort': 'date_asc'},
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            shows = payload.get('shows') if payload.get('success') else None
            if not isinstance(shows, list):
                raise ValueError('Shows API response does not contain a shows list')
            for show in shows:
                if isinstance(show, dict) and show.get('type') == 'SHOW':
                    if status == 'PUBLISHED' and show.get('slug'):
                        detail_url = urljoin(
                            SOURCE_URL, f'api/shows/by-slug/{show["slug"]}'
                        )
                        try:
                            detail_response = session.get(detail_url, timeout=45)
                            detail_response.raise_for_status()
                            detail_payload = detail_response.json()
                            detail = detail_payload.get('show')
                            if detail_payload.get('success') and isinstance(detail, dict):
                                show = {**show, **detail}
                        except (requests.RequestException, ValueError) as error:
                            log_message(
                                'Failed to scrape Salisbury Symphony show detail',
                                event='crawler_item_failed',
                                level='warning',
                                url=detail_url,
                                error_type=type(error).__name__,
                                error_message=str(error),
                            )
                    records.extend(show_records(show))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape Salisbury Symphony shows feed',
                event='crawler_feed_failed',
                level='warning',
                url=SHOWS_API,
                status=status,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'], record['title']),
    )


class SalisburySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='salisburysymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SalisburySymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
