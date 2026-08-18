import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bso.org/'
SOURCE = 'Boston Symphony Orchestra'
SEARCH_URL = 'https://go8f04wi19tuvlyrp-1.a1.typesense.net/multi_search'
API_KEY = 'qoWHCTjesGfIaxdXbw9vOgod1VToEXNI'
PAGE_SIZE = 250
LOCAL_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_html(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def main_venue(document):
    venues = document.get('performance_venue') or []
    return next((item for item in venues if item.get('main_venue')), venues[0] if venues else {})


def city_from_venue(venue):
    location = clean_html(venue.get('location'))
    city = location.split(',', 1)[0].strip()
    # Tanglewood's site labels its campus location "Lenox/Stockbridge". Its
    # performance venues and mailing address are in Lenox.
    if city == 'Lenox/Stockbridge':
        return 'Lenox'
    return city


def description_from_document(document):
    parts = []
    for field in ('subhead', 'excerpt', 'content_keywords'):
        text = clean_html(document.get(field))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def record_from_document(document):
    title = clean_html(document.get('title'))
    link = document.get('performance_link') or document.get('event_link')
    timestamp = document.get('performance_date')
    venue = main_venue(document)
    venue_name = clean_html(venue.get('name'))
    city = city_from_venue(venue)

    try:
        moment = datetime.fromtimestamp(int(timestamp), tz=LOCAL_TIMEZONE)
    except (TypeError, ValueError, OverflowError, OSError):
        return None

    if not title or not link or not venue_name or not city or moment.year < 1900:
        return None

    url = urljoin(SOURCE_URL, link)
    return {
        'title': title,
        'date': moment.date().isoformat(),
        'url': url,
        'time_from': moment.strftime('%H:%M'),
        'venue': venue_name,
        'city': city,
        'country_code': 'US',
        'description': description_from_document(document),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def search_page(session, page):
    payload = {
        'searches': [{
            'collection': 'performances',
            'q': '*',
            'query_by': 'title,excerpt,subhead,content_keywords,performance_categories',
            'filter_by': 'show_in_cal:true && performance_date:>0',
            'sort_by': 'performance_date:asc',
            'page': page,
            'per_page': PAGE_SIZE,
        }]
    }
    response = session.post(
        SEARCH_URL,
        params={'x-typesense-api-key': API_KEY},
        json=payload,
        timeout=45,
    )
    response.raise_for_status()
    results = response.json().get('results') or []
    return results[0] if results else {}


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        result = search_page(session, page)
        hits = result.get('hits') or []
        for hit in hits:
            record = record_from_document(hit.get('document') or {})
            if record:
                records.append(record)

        found = int(result.get('found') or 0)
        if not hits or page * PAGE_SIZE >= found:
            break
        page += 1

    if not records:
        log_message(
            'No BSO calendar performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SEARCH_URL,
            record_count=0,
        )

    return records


class BsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bso_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BsoOrgCrawler().run()


if __name__ == '__main__':
    main()
