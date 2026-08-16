import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://earlymusicseattle.org/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/events'
SOURCE = 'Early Music Seattle'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def listing_events(session):
    """Return every published event, including the site's retained archive."""
    records = []
    page = 1
    while True:
        response = get_json(
            session,
            EVENTS_API,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link,title,content,meta',
            },
        )
        records.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return records
        page += 1


def city_from_address(address):
    # Detail pages consistently use US postal addresses such as
    # "609 8th Ave, Seattle, WA 98104". Keep the location source-driven.
    match = re.search(r',\s*([^,]+),\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\s*$', address)
    if not match:
        return ''
    city = clean_text(match.group(1))
    # A few source addresses omit the comma after a suite number, e.g.
    # "STE 220 Seattle, WA". The state-delimited final token is still clear.
    city = re.sub(r'^(?:STE|SUITE)\s+[A-Z0-9-]+\s+', '', city, flags=re.IGNORECASE)
    return city


def detail_location(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    venue_node = soup.select_one('.event-data .venue, .venue')
    address_node = soup.select_one('.event-data .venue_address, .venue_address')
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
    address = clean_text(address_node.get_text(' ', strip=True) if address_node else '')
    return venue, city_from_address(address)


def make_record(event, venue, city):
    meta = event.get('meta') or {}
    start = meta.get('_piecal_start_date') or ''
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2})(?::\d{2})?)?', start)
    title = clean_text((event.get('title') or {}).get('rendered'))
    url = event.get('link') or ''
    if not match or not title or not url or not venue or not city:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None
    if not meta.get('_piecal_is_allday') and match.group(2):
        time_from = f'{match.group(2)}:{match.group(3)}'

    description = clean_text((event.get('content') or {}).get('rendered')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_location, session, event.get('link', '')): event
            for event in events if event.get('link')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                venue, city = future.result()
                record = make_record(event, venue, city)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                record = None
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class EarlyMusicSeattleOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='earlymusicseattle_org',
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
    EarlyMusicSeattleOrgCrawler().run()


if __name__ == '__main__':
    main()
