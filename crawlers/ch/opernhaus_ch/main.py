import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opernhaus.ch/'
CALENDAR_URL = urljoin(SOURCE_URL, 'spielplan/kalendarium/')
SOURCE = 'Opernhaus Zürich'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_pages(session):
    """Yield every server-rendered page used by the infinite-scroll calendar."""
    url = CALENDAR_URL
    seen = set()
    while url and url not in seen:
        seen.add(url)
        soup = get_soup(session, url)
        yield soup
        next_link = soup.select_one('.js-pager .MarkupPagerNavNext a[href]')
        url = urljoin(url, next_link['href']) if next_link else None


def json_events(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for event in values:
            if isinstance(event, dict) and event.get('@type') == 'Event':
                yield event


def resolve_location(event):
    location = event.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country = clean_text(address.get('addressCountry')).upper()

    # The site's JSON-LD currently copies the Zurich postal address into its
    # explicitly labelled Edinburgh tour dates. Do not turn that tour into a
    # Zurich performance.
    combined = f"{event.get('name', '')} {venue} {event.get('description', '')}"
    if re.search(r'\bEdinburgh\b', combined, re.I):
        return venue or 'Edinburgh Festival', 'Edinburgh', 'GB'

    # Other explicitly touring entries are unsafe when the copied home address
    # is the only geographic evidence available.
    if re.search(r'\bGastspiel\b', combined, re.I) and city == 'Zürich':
        return None

    if not venue or not city:
        return None
    return venue, city, country or 'CH'


def event_record(event):
    title = clean_text(event.get('name'))
    url = urljoin(SOURCE_URL, event.get('url') or '')
    start = event.get('startDate') or ''
    try:
        parsed = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    location = resolve_location(event)
    if not title or not url or not location:
        return None
    venue, city, country_code = location
    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': url,
        'time_from': parsed.strftime('%H:%M') if 'T' in start else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    for selector in ('.content-section.detail', '.cp-sitecontent .page-content-container'):
        for node in soup.select(selector):
            text = clean_text(node.get_text('\n', strip=True))
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = {}
    for soup in calendar_pages(session):
        for event in json_events(soup):
            record = event_record(event)
            if not record:
                continue
            key = (record['url'], record['date'], record['time_from'], record['venue'])
            records[key] = record

    descriptions = {}
    urls = {record['url'] for record in records.values()}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records.values():
        detail = descriptions.get(record['url'])
        summary = record['description']
        if detail and summary and summary not in detail:
            record['description'] = f'{summary}\n\n{detail}'
        elif detail:
            record['description'] = detail

    return sorted(
        records.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class OpernhausChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opernhaus_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
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
        return get_concerts()


def main():
    OpernhausChCrawler().run()


if __name__ == '__main__':
    main()
