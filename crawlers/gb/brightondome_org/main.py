import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://brightondome.org/'
LISTING_URL = f'{SOURCE_URL}whats-on/'
SOURCE = 'Brighton Dome'
COUNTRY_CODE = 'GB'
TIMEZONE = ZoneInfo('Europe/London')

# The first-party taxonomy is broad. These are candidate categories capable of
# containing events within the project's classical/crossover definition.
CANDIDATE_GENRES = {'181', '85', '88', '91', '179'}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw or '&' in raw
        else raw
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def stream_url(session):
    html = get_response(session, LISTING_URL).text
    match = re.search(
        r"_filter_streaming\s*=\s*\{\s*data_url:\s*'([^']+)'", html
    )
    if not match:
        raise ValueError('Could not locate the first-party event stream URL')
    return match.group(1)


def listing_events(session):
    url = stream_url(session)
    offset = 0
    limit = 200
    events = []
    while True:
        page = get_response(
            session, url, params={'offset': offset, 'limit': limit}
        ).json()
        if not isinstance(page, list):
            raise ValueError('Unexpected event stream response')
        events.extend(page)
        if len(page) < limit:
            break
        offset += len(page)

    return [
        event for event in events
        if CANDIDATE_GENRES.intersection(map(str, event.get('genre') or []))
    ]


def event_schema(session, url):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def location(schema, event):
    place = schema.get('location') or {}
    address = place.get('address') or {}
    venue = clean_text(place.get('name')) or clean_text(event.get('venue_name'))
    city = clean_text(address.get('addressLocality'))
    country = clean_text(address.get('addressCountry')).lower()
    if not city and venue:
        # This is Brighton Dome's venue calendar; its selectable rooms and
        # partner listings are all Brighton venues.
        city = 'Brighton'
    if country and country not in {'united kingdom', 'gb', 'uk'}:
        return None, None
    return venue or None, city or None


def make_records(event, schema):
    title = clean_text(schema.get('name') or event.get('name'))
    url = clean_text(event.get('url'))
    venue, city = location(schema, event)
    description = clean_text(schema.get('description')) or clean_text(event.get('text')) or None
    timestamps = event.get('performance_times') or event.get('dates') or []
    if not title or not url or not venue or not city:
        return []

    records = []
    for timestamp in timestamps:
        try:
            moment = datetime.fromtimestamp(int(timestamp), tz=TIMEZONE)
        except (TypeError, ValueError, OSError):
            continue
        records.append({
            'title': title,
            'date': moment.date().isoformat(),
            'url': url,
            'time_from': moment.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(event_schema, session, event.get('url')): event
            for event in events if event.get('url')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                schema = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('url'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                schema = {}
            records.extend(make_records(event, schema))

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class BrightonDomeOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brightondome_org',
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
    BrightonDomeOrgCrawler().run()


if __name__ == '__main__':
    main()
