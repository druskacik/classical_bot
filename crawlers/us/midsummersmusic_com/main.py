from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.midsummersmusic.com/'
SOURCE = "Midsummer's Music"
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
PAGE_SIZE = 50
START_DATE = '2000-01-01 00:00:00'
END_DATE = '2100-12-31 23:59:59'
PRIVATE_CATEGORIES = {'gsq-private', 'mm-private'}

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
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('script, style'):
        element.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    lines = [' '.join(line.split()) for line in text.splitlines()]
    text = '\n'.join(line for line in lines if line).strip()
    return text or None


def parse_start(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def event_record(event):
    category_slugs = {
        category.get('slug') for category in event.get('categories', [])
    }
    if category_slugs & PRIVATE_CATEGORIES:
        return None

    start = parse_start(event.get('start_date'))
    venue_data = event.get('venue') or {}
    title = clean_html(event.get('title'))
    url = (event.get('url') or '').strip()
    venue = clean_html(venue_data.get('venue'))
    city = clean_html(venue_data.get('city'))
    if not all((title, start, url, venue, city)):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_html(event.get('description')),
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    skipped_count = 0
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'start_date': START_DATE,
                'end_date': END_DATE,
                'per_page': PAGE_SIZE,
                'page': page,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []

        for event in events:
            record = event_record(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        total_pages = int(payload.get('total_pages') or 0)
        if not events or page >= total_pages:
            break
        page += 1

    if skipped_count:
        log_message(
            'Skipped private or incomplete Midsummer\'s Music events',
            event='crawler_items_skipped',
            level='warning',
            record_count=skipped_count,
        )

    unique = {(record['url'], record['date'], record['time_from']): record for record in records}
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class MidsummersMusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='midsummersmusic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    return MidsummersMusicComCrawler().run()


if __name__ == '__main__':
    main()
