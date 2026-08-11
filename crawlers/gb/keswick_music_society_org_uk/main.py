from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://keswick-music-society.org.uk/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Keswick Music Society'
CITY = 'Keswick'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if value is None:
        return ''
    return ' '.join(unescape(str(value)).replace('\xa0', ' ').split())


def description_text(value):
    if not value:
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for br in soup.find_all('br'):
        br.replace_with('\n')
    lines = [clean_text(line) for line in soup.get_text('\n').splitlines()]
    description = '\n'.join(line for line in lines if line)
    return description or None


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue')
    venue = clean_text(venue_data.get('venue')) if isinstance(venue_data, dict) else ''

    # The API also contains administrative placeholders and a few old records
    # for which no venue was published. Neither is a defensible occurrence.
    if not title or not url or not venue:
        return None

    try:
        start = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError) as error:
        log_message(
            'Skipping Keswick Music Society event with invalid date',
            event='crawler_item_skipped',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    time_from = None if event.get('all_day') else start.strftime('%H:%M')
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': description_text(event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    page = 1
    records = []

    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01',
                'end_date': '2100-01-01',
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

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class KeswickMusicSocietyOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='keswick_music_society_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
    KeswickMusicSocietyOrgUkCrawler().run()


if __name__ == '__main__':
    main()
