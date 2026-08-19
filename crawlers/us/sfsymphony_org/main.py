import re
from datetime import datetime
from html import unescape
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sfsymphony.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'Calendar')
SOURCE = 'San Francisco Symphony'
ALGOLIA_URL = 'https://3zvewsxvk4-1.algolianet.com/1/indexes/*/queries'
ALGOLIA_APP_ID = '3ZVEWSXVK4'
ALGOLIA_API_KEY = 'e6c0617a0995d310c9dd600df5af93c2'
INDEX_NAME = 'prod_sfs_calendar'
PAGE_SIZE = 100

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
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def list_text(values):
    cleaned = []
    for value in values or []:
        text = clean_text(value)
        if text and text.lower() != 'to be announced' and text not in cleaned:
            cleaned.append(text)
    return '; '.join(cleaned)


def build_description(hit):
    parts = []
    for label, field in (
        ('Artists', 'artists'),
        ('Conductors', 'conductors'),
        ('Composers', 'composers'),
        ('Programme', 'works'),
        ('Series', 'series'),
        ('Concert types', 'Concert Type'),
    ):
        value = list_text(hit.get(field))
        if value:
            parts.append(f'{label}: {value}')
    return '\n\n'.join(parts) or None


def resolve_location(hit, title, url):
    venue = clean_text(hit.get('venue'))
    if venue == 'Display Name':
        if 'Shoreline-4th-of-July' in url:
            return 'Shoreline Amphitheatre', 'Mountain View'
        if 'Stern-Grove' in url:
            return 'Stern Grove', 'San Francisco'
        if 'Midsummer-Nights-Dream' in url or 'James-Bond' in url:
            return 'Davies Symphony Hall', 'San Francisco'
        return None

    if venue == 'Youth Orchestra':
        return 'Davies Symphony Hall', 'San Francisco'

    if not venue and title.startswith('Free Community Performance:'):
        inferred = clean_text(title.split(':', 1)[1])
        if inferred:
            return inferred, 'San Francisco'

    if not venue:
        return None
    return venue, 'San Francisco'


def parse_hit(hit):
    title = clean_text(hit.get('title'))
    path = hit.get('kenticoUrl')
    raw_datetime = hit.get('performanceDate')
    if not title or not path or not raw_datetime:
        return None

    try:
        performance = datetime.fromisoformat(raw_datetime)
    except (TypeError, ValueError):
        return None

    url = urljoin(SOURCE_URL, path)
    location = resolve_location(hit, title, url)
    if not location:
        return None
    venue, city = location

    return {
        'title': title,
        'date': performance.date().isoformat(),
        'url': url,
        'time_from': None if hit.get('hideTime') else performance.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': build_description(hit),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_page(session, page):
    params = urlencode({
        'hitsPerPage': PAGE_SIZE,
        'page': page,
        'facetFilters': '[["excludeFromCalendar:false"]]',
    })
    response = session.post(
        ALGOLIA_URL,
        params={
            'x-algolia-application-id': ALGOLIA_APP_ID,
            'x-algolia-api-key': ALGOLIA_API_KEY,
        },
        json={'requests': [{'indexName': INDEX_NAME, 'params': params}]},
        timeout=45,
    )
    response.raise_for_status()
    results = response.json().get('results') or []
    if not results:
        raise ValueError('Algolia response did not contain results')
    return results[0]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    skipped_count = 0
    page = 0

    while True:
        result = fetch_page(session, page)
        for hit in result.get('hits') or []:
            record = parse_hit(hit)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        page += 1
        if page >= int(result.get('nbPages') or 0):
            break

    if skipped_count:
        log_message(
            'Skipped calendar entries missing required event data',
            event='crawler_records_skipped',
            level='warning',
            url=CALENDAR_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No calendar performances found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SfSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfsymphony_org',
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
        return scrape_concerts()


def main():
    SfSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
