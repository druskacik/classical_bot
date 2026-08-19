import json
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sfopera.com/'
SOURCE = 'San Francisco Opera'
CALENDAR_API = urljoin(SOURCE_URL, 'ace-api/events')
CITY = 'San Francisco'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    return ' '.join(str(value).replace('\xa0', ' ').split())


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') in {'Event', 'EventSeries'}:
                return item
    return {}


def schema_location(schema):
    location = schema.get('location') or {}
    if not isinstance(location, dict):
        return '', ''
    address = location.get('address') or {}
    city = address.get('addressLocality') if isinstance(address, dict) else ''
    return clean_text(location.get('name')), clean_text(city)


def detail_data(session, url, cache):
    if url in cache:
        return cache[url]
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        schema = event_schema(soup)
        venue, city = schema_location(schema)
        description = clean_text(schema.get('description'))
        if not description:
            main = soup.select_one('main')
            description = clean_text(main.get_text(' ', strip=True)) if main else ''

        # This calendar entry predates the site's structured event markup, but
        # its first-party page explicitly identifies the performance location.
        if url.rstrip('/').endswith('/seasons/opera-in-the-park'):
            venue, city = 'Robin Williams Meadow, Golden Gate Park', CITY

        cache[url] = (venue, city, description or None)
    except requests.RequestException as error:
        log_message(
            'Unable to fetch event detail',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        cache[url] = ('', '', None)
    return cache[url]


def parse_event_date(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.date().isoformat(), parsed.strftime('%H:%M')
    except (TypeError, ValueError):
        return '', None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(
        CALENDAR_API,
        params={'startDate': '1900-01-01', 'endDate': '2100-01-01'},
        timeout=60,
    )
    response.raise_for_status()
    events = response.json()
    cache = {}
    unresolved_urls = set()
    records = []

    for event in events:
        title = clean_text(event.get('name'))
        date, time_from = parse_event_date(event.get('eventDate'))
        path = event.get('viewDetailCtaUrl') or event.get('buyTicketCtaUrl')
        url = urljoin(SOURCE_URL, path or '')
        if not title or not date or not path or not url.startswith(SOURCE_URL):
            continue

        venue, city, detail_description = detail_data(session, url, cache)
        if not venue or not city:
            if url not in unresolved_urls:
                log_message(
                    'Skipping events without a resolved venue',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                    error_type='MissingVenue',
                )
                unresolved_urls.add(url)
            continue

        api_description = clean_text(event.get('synopsis') or event.get('subTitle'))
        description = detail_description or api_description or None
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
        })

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SfOperaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfopera_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SfOperaComCrawler().run()


if __name__ == '__main__':
    main()
