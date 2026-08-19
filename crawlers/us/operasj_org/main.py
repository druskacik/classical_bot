import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operasj.org/'
SOURCE = 'Opera San José'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
CITY = 'San Jose'
MAINSTAGE_VENUE = 'The California Theatre'
PER_PAGE = 50

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_events(session):
    page = 1
    while True:
        params = {
            'per_page': PER_PAGE,
            'page': page,
            # Supplying a range is required: without it, the API returns only
            # upcoming events. The published archive currently begins in 2023.
            'start_date': '1900-01-01 00:00:00',
            'end_date': f'{date.today().year + 5}-12-31 23:59:59',
            'status': 'publish',
        }
        response = session.get(API_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []
        yield from events

        total_pages = int(payload.get('total_pages') or 0)
        if page >= total_pages:
            break
        page += 1


def parse_event(event):
    title = clean_text(event.get('title'))
    url = (event.get('url') or '').strip()
    start = (event.get('start_date') or '').strip()
    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except (TypeError, ValueError):
        return None

    time_match = re.search(r'\b(\d{2}:\d{2}):\d{2}\b', start)
    category_slugs = {
        str(category.get('slug', '')).lower()
        for category in event.get('categories') or []
        if isinstance(category, dict)
    }
    venue_data = event.get('venue')
    venue_data = venue_data if isinstance(venue_data, dict) else {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    # Opera San José identifies its "Performances" category as mainstage
    # productions. Its own site states that those productions are presented at
    # the California Theatre, although many API rows omit their venue object.
    if not venue and 'performances' in category_slugs:
        venue = MAINSTAGE_VENUE
        city = CITY
    elif venue and not city:
        city = CITY

    # Online-only premieres are not live performances. Other uncategorized
    # items without a location cannot meet the required venue contract.
    if venue.lower() == 'virtual' or not all((title, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(1) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    skipped_count = 0
    for event in api_events(session):
        record = parse_event(event)
        if record:
            records.append(record)
        else:
            skipped_count += 1

    if skipped_count:
        log_message(
            'Skipped calendar entries missing required concert fields or marked virtual',
            event='crawler_items_skipped',
            level='info',
            skipped_count=skipped_count,
        )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class OperasjOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operasj_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        # The broad calendar contains untagged community events, talks, and a
        # dinner alongside eligible opera performances. Classification is
        # therefore required even though the presenting company is operatic.
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperasjOrgCrawler().run()


if __name__ == '__main__':
    main()
