import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://teatrodellago.cl/'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Teatro del Lago'
DEFAULT_VENUE = 'Teatro del Lago'
DEFAULT_CITY = 'Frutillar'

# Cloudflare serves the public site and API to search crawlers while presenting
# its JavaScript challenge to generic automated HTTP clients.
HEADERS = {
    'User-Agent': 'Googlebot',
    'Accept': 'application/json',
    'Accept-Language': 'es-CL,es;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    params = {
        'per_page': 50,
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
        'status': 'publish',
    }
    url = EVENTS_API
    events = []

    while url:
        response = session.get(url, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('events') or [])
        url = payload.get('next_rest_url')
        params = None

    return events


def resolve_location(event):
    venue_data = event.get('venue')
    if isinstance(venue_data, dict):
        venue = clean_text(venue_data.get('venue'))
        city = clean_text(venue_data.get('city')) or DEFAULT_CITY
        country = clean_text(venue_data.get('country'))
        if country and country.casefold() not in ('chile', 'cl'):
            return None, None
        if venue:
            return venue, city

    # This is a venue-specific programme. Entries without a venue object are
    # still presented as Teatro del Lago events in Frutillar.
    return DEFAULT_VENUE, DEFAULT_CITY


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = clean_text(event.get('start_date'))
    venue, city = resolve_location(event)
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}', start)
    if not title or not url or not match or not venue or not city:
        return None

    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None if event.get('all_day') else f'{match.group(2)}:{match.group(3)}'
    description = clean_text(event.get('description') or event.get('excerpt')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'CL',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class TeatroDelLagoClCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrodellago_cl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CL',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Teatro del Lago events API',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for event in events:
            record = make_record(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped invalid Teatro del Lago event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=clean_text(event.get('url')),
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    TeatroDelLagoClCrawler().run()


if __name__ == '__main__':
    main()
