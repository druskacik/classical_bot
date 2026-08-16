import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lowellchamberorchestra.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Lowell Chamber Orchestra'
TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

ADDRESS_VENUES = {
    '240 central street': 'Richard and Nancy Donahue Academic Arts Center',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def add_json_format(url):
    parsed = urlparse(urljoin(SOURCE_URL, url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunparse(parsed._replace(query=urlencode(query)))


def local_datetime(milliseconds):
    if not isinstance(milliseconds, (int, float)):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).astimezone(TIMEZONE)


def venue_and_city(item):
    location = item.get('location') or {}
    address = clean_text(location.get('addressLine1'))
    venue = clean_text(location.get('addressTitle'))
    city = clean_text(location.get('addressLine2')).split(',', 1)[0].strip()

    inferred_venue = ADDRESS_VENUES.get(address.lower())
    if inferred_venue and (not venue or venue == SOURCE):
        venue = inferred_venue
    return venue, city


def description_for(item):
    parts = []
    for value in (item.get('excerpt'), item.get('body')):
        text = clean_text(value)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def record_from_item(item):
    title = clean_text(item.get('title'))
    event_datetime = local_datetime(item.get('startDate'))
    venue, city = venue_and_city(item)
    path = clean_text(item.get('fullUrl'))

    if not title or not event_datetime or not venue or not city or not path:
        return None

    return {
        'title': title,
        'date': event_datetime.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': event_datetime.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'description': description_for(item),
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    page_url = add_json_format(EVENTS_URL)
    seen_pages = set()
    records = []

    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
        for item in items:
            record = record_from_item(item)
            if record:
                records.append(record)

        pagination = payload.get('pagination') or {}
        next_url = pagination.get('nextPageUrl') if pagination.get('nextPage') else None
        page_url = add_json_format(next_url) if next_url else None

    if not records:
        log_message(
            'No valid events found in collection',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class LowellChamberOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lowellchamberorchestra_org',
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
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return scrape_events()


def main():
    LowellChamberOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
