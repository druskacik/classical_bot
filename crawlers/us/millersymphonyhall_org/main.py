import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://millersymphonyhall.org/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/event'
SOURCE = 'Miller Symphony Hall'
DEFAULT_VENUE = 'Miller Symphony Hall'
DEFAULT_CITY = 'Allentown'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The event post type does not expose its ACF date or location fields through
# REST. Most events use the hall named by this venue calendar; these are the
# explicitly identified off-site locations found in event descriptions.
OFFSITE_VENUES = (
    ('moravian university’s peter hall', 'Peter Hall, Moravian University', 'Bethlehem'),
    ("moravian university's peter hall", 'Peter Hall, Moravian University', 'Bethlehem'),
    ('roosevelt elementary school', 'Roosevelt Elementary School', 'Allentown'),
    ('church of the mediator', 'Church of the Mediator', 'Allentown'),
    ('rodale community room', 'Rodale Community Room, Miller Symphony Hall', 'Allentown'),
    ('allen organ company', 'Allen Organ Company', 'Macungie'),
    ('americus hotel', 'Americus Hotel', 'Allentown'),
    ('lehigh country club', 'Lehigh Country Club', 'Allentown'),
)

DATE_FORMAT = '%A, %B %d, %Y @ %I:%M %p'


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json(), response.headers


def listing_events(session):
    events = []
    page = 1
    while True:
        payload, headers = get_json(
            session,
            EVENTS_API,
            params={
                'page': page,
                'per_page': 100,
                'status': 'publish',
                '_fields': 'id,link,title,content',
            },
        )
        events.extend(payload)
        if page >= int(headers.get('X-WP-TotalPages', 1)):
            return events
        page += 1


def resolve_location(description):
    folded = description.casefold()
    if 'join us online' in folded or 'online-only' in folded:
        return None, None
    for marker, venue, city in OFFSITE_VENUES:
        if marker in folded:
            return venue, city
    return DEFAULT_VENUE, DEFAULT_CITY


def detail_data(html):
    soup = BeautifulSoup(html, 'html.parser')
    frame = soup.select_one('main .frame')
    if not frame:
        return '', []

    occurrences = []
    for element in frame.select('ul.event-info li b'):
        value = clean_text(element)
        try:
            occurrences.append(datetime.strptime(value, DATE_FORMAT))
        except ValueError:
            continue

    heading = frame.find('h2')
    subtitle = clean_text(heading.select_one('.subtitle')) if heading else ''
    return subtitle, occurrences


def make_records(event, html):
    title = clean_text((event.get('title') or {}).get('rendered'))
    description = clean_text((event.get('content') or {}).get('rendered'))
    url = event.get('link') or ''
    subtitle, occurrences = detail_data(html)
    if subtitle and subtitle.casefold() not in title.casefold():
        title = f'{title} – {subtitle}'
    if not title or not url:
        return []

    venue, city = resolve_location(description)
    if not venue or not city:
        return []
    return [
        {
            'title': title,
            'date': occurrence.date().isoformat(),
            'url': url,
            'time_from': occurrence.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for occurrence in occurrences
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(session.get, event['link'], timeout=45): event
            for event in events
            if event.get('link')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                response = future.result()
                response.raise_for_status()
                event_records = make_records(event, response.text)
                if not event_records:
                    log_message(
                        'Event had no parseable occurrences',
                        event='crawler_item_skipped',
                        level='warning',
                        url=event.get('link'),
                    )
                records.extend(event_records)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class MillerSymphonyHallOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='millersymphonyhall_org',
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
        return get_concerts()


def main():
    MillerSymphonyHallOrgCrawler().run()


if __name__ == '__main__':
    main()
