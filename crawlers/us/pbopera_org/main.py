import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pbopera.org/'
EVENT_SITEMAP_URL = urljoin(SOURCE_URL, 'event-sitemap.xml')
SOURCE = 'Palm Beach Opera'

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
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    root = ElementTree.fromstring(get_response(session, EVENT_SITEMAP_URL).content)
    namespace = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    return [
        node.text.strip()
        for node in root.findall('.//sm:url/sm:loc', namespace)
        if node.text and '/event/' in node.text
    ]


def event_objects(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            # Some current pages contain literal newlines in JSON-LD strings.
            # Browsers accept them, and the non-strict decoder preserves the
            # otherwise valid structured event data.
            payload = json.loads(script.string or script.get_text(), strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                events.append(item)
    return events


def parse_start(value):
    if not isinstance(value, str):
        return None, None
    try:
        start = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return start.date().isoformat(), start.strftime('%H:%M')


def location_fields(event):
    location = event.get('location') or {}
    if not isinstance(location, dict):
        return None, None
    address = location.get('address') or {}
    if not isinstance(address, dict):
        address = {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    return venue or None, city or None


def page_description(soup):
    content = soup.select_one('.vem-single-event-content') or soup.select_one('main')
    return clean_text(content) or None


def page_records(session, url):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    description = page_description(soup)
    records = []
    for event in event_objects(soup):
        title = clean_text(event.get('name'))
        event_date, time_from = parse_start(event.get('startDate'))
        venue, city = location_fields(event)
        if not title or not event_date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or clean_text(event.get('description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(page_records, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Palm Beach Opera event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class PboperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pbopera_org',
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
        return get_concerts()


def main():
    PboperaOrgCrawler().run()


if __name__ == '__main__':
    main()
