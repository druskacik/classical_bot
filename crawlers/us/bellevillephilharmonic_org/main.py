import html
import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bellevillephilharmonic.org/'
LISTING_URL = urljoin(SOURCE_URL, 'events/list/')
PAST_URL = f'{LISTING_URL}?eventDisplay=past'
SOURCE = 'Philharmonic Society of Belleville'

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
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = re.sub(r'[ \t]+', ' ', text.replace('\xa0', ' '))
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_objects(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict) and value.get('@type') == 'Event':
                yield value


def parse_datetime(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_event(event):
    start = parse_datetime(event.get('startDate'))
    location = event.get('location') if isinstance(event.get('location'), dict) else {}
    address = location.get('address') if isinstance(location.get('address'), dict) else {}
    title = clean_text(event.get('name'))
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    url = urljoin(SOURCE_URL, event.get('url') or '')

    if (
        not start or not title or not venue or not city
        or urlparse(url).netloc != urlparse(SOURCE_URL).netloc
    ):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('eventAttendanceMode') == 'https://schema.org/OnlineEventAttendanceMode' else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def listing_pages(session, start_url):
    url = start_url
    seen = set()
    for _ in range(100):
        if url in seen:
            break
        seen.add(url)
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        events = list(event_objects(soup))
        if not events:
            break
        yield events

        next_url = None
        for link in soup.select('a[href]'):
            label = clean_text(link.get_text(' ', strip=True)).lower()
            href = urljoin(url, link.get('href'))
            if label == 'previous events' and 'eventDisplay=past' in href:
                next_url = href
                break
            if label == 'next events' and 'eventDisplay=past' not in href and start_url == LISTING_URL:
                next_url = href
                break
        if not next_url:
            break
        url = next_url


def detail_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return next(event_objects(soup), None)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    summaries = {}

    for start_url in (LISTING_URL, PAST_URL):
        for page in listing_pages(session, start_url):
            for event in page:
                key = (event.get('url'), event.get('startDate'))
                summaries[key] = event

    records = []
    for summary in summaries.values():
        url = urljoin(SOURCE_URL, summary.get('url') or '')
        try:
            event = detail_event(session, url) or summary
        except requests.RequestException as error:
            log_message(
                'Event detail request failed; using listing data',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            event = summary
        record = parse_event(event)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No valid events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BellevillePhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bellevillephilharmonic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BellevillePhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
