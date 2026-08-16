import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musicinthemountains.org/'
LISTING_URL = f'{SOURCE_URL}concerts-and-events'
COLLECTION_URL = (
    f'{SOURCE_URL}rts/collections/public/8514d867/runtime/collection/'
    'crm-events/query-data'
)
SOURCE = 'Music in the Mountains'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': LISTING_URL,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        parsed = date.fromisoformat(str(value).split('T', 1)[0])
    except (TypeError, ValueError):
        return None
    return parsed.isoformat()


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).strftime('%H:%M')
    except ValueError:
        return None


def is_concert(data):
    categories = {
        item.strip().casefold()
        for item in str(data.get('Event Category Name') or '').split('||')
        if item.strip()
    }
    return data.get('Event Web Publish') == 'Yes' and 'concerts' in categories


def fetch_collection(session):
    values = []
    page_number = 0

    while True:
        response = session.get(
            COLLECTION_URL,
            params={
                'pageSize': 100,
                'pageNumber': page_number,
                'query': '()',
                'language': 'ENGLISH',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        values.extend(payload.get('values') or [])

        page = payload.get('page') or {}
        total_pages = int(page.get('totalPages') or 1)
        page_number += 1
        if page_number >= total_pages:
            return values


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    # Establish the first-party page as referer; the collection endpoint rejects
    # bare clients but does not require authentication.
    listing_response = session.get(LISTING_URL, timeout=45)
    listing_response.raise_for_status()

    records = []
    for item in fetch_collection(session):
        data = item.get('data') or {}
        if not is_concert(data):
            continue

        title = clean_text(data.get('Event Name'))
        event_date = parse_date(data.get('Event Start Date'))
        url = str(data.get('Event Details URL') or '').strip()
        venue = clean_text(data.get('Event Location Name'))
        city = clean_text(data.get('City'))
        if not all((title, event_date, url, venue, city)):
            log_message(
                'Skipping incomplete concert occurrence',
                event='crawler_record_skipped',
                level='warning',
                url=url or LISTING_URL,
                error_type='IncompleteRecord',
                error_message='Missing title, date, URL, venue, or city',
            )
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(
                data.get('Event Start Datetime') or data.get('Event Start Time')
            ),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': clean_text(data.get('Event Description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No published concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MusicInTheMountainsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicinthemountains_org',
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
        return scrape_concerts()


def main():
    MusicInTheMountainsOrgCrawler().run()


if __name__ == '__main__':
    main()
