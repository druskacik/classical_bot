import json
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.charlottesymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'whats-on')
SOURCE = 'Charlotte Symphony Orchestra'

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
    return ' '.join(str(value).replace('\xa0', ' ').split())


def event_objects(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in payload if isinstance(payload, list) else [payload]:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                yield item


def listing_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            LISTING_URL, params={'max': 36, 'page': page}, timeout=45
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = []
        for link in soup.select('a.desc[href*="/whats-on/"]'):
            url = urljoin(LISTING_URL, link.get('href'))
            if url not in page_urls:
                page_urls.append(url)
        urls.extend(url for url in page_urls if url not in urls)

        pages = [
            int(option.get('value'))
            for option in soup.select('#pagination-select option[value]')
            if option.get('value', '').isdigit()
        ]
        if not page_urls or page >= max(pages, default=page):
            break
        page += 1
    return urls


def description_from_page(soup, event):
    parts = []
    for node in soup.select(
        '.desc1Wrapper .richtext, .desc1Wrapper .extraInfo, '
        '.programmeWrapper .richtext'
    ):
        value = clean_text(node.get_text(' ', strip=True))
        if value and value not in parts:
            parts.append(value)
    summary = clean_text(event.get('description'))
    if summary and not any(summary.rstrip('…') in part for part in parts):
        parts.insert(0, summary)
    return '\n\n'.join(part for part in parts if part) or None


def record_from_event(event, url, description):
    title = clean_text(event.get('name'))
    start = event.get('startDate')
    location = event.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    try:
        moment = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    if not title or not venue or not city:
        return None
    return {
        'title': title,
        'date': moment.date().isoformat(),
        'url': url,
        'time_from': moment.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in listing_urls(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            events = list(event_objects(soup))
            for event in events:
                record = record_from_event(
                    event, url, description_from_page(soup, event)
                )
                if record:
                    records.append(record)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class CharlotteSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='charlottesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CharlotteSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
