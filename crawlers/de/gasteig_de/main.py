import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gasteig.de/'
CALENDAR_URL = f'{SOURCE_URL}veranstaltungen/'
AJAX_URL = f'{SOURCE_URL}backend/admin-ajax.php'
SOURCE = 'Gasteig München'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
    'Referer': CALENDAR_URL,
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def listing_metadata(session):
    response = session.get(CALENDAR_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    for script in soup.select('script[type="application/json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            payload.get('post_type') == 'events'
            and str(payload.get('duplicates')) == '1'
            and payload.get('count') is not None
        ):
            return int(payload['count'])
    raise ValueError('Event listing metadata was not found')


def listing_page(session, total_count):
    response = session.post(
        AJAX_URL,
        data={
            'post_type': 'events',
            'order': 'newest',
            'type': 'automated',
            'time_span': '0',
            'duplicates': '1',
            'gridType': 'fullWidth',
            'section': 'true',
            'show': str(total_count),
            'language': 'de',
            'current': '0',
            'past_events': 'false',
            'action': 'load_teasers',
            'count': str(total_count),
            'cache': 'false',
            'ignore': '[]',
            'listView': 'false',
        },
        timeout=60,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    teasers = soup.select('[data-component="teaser"]')
    urls = []
    for teaser in teasers:
        link = teaser.select_one('a[href*="/veranstaltungen/"]')
        if link and link.get('href'):
            urls.append(link['href'])
    return urls, len(teasers)


def listing_urls(session):
    total_count = listing_metadata(session)
    urls, _ = listing_page(session, total_count)
    return list(dict.fromkeys(urls))


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload.get('@graph', []) if isinstance(payload, dict) else []
        if isinstance(payload, dict):
            candidates = [payload, *candidates]
        for item in candidates:
            item_type = item.get('@type') if isinstance(item, dict) else None
            if item_type == 'Event' or (isinstance(item_type, list) and 'Event' in item_type):
                return item
    return None


def description_from(soup, schema):
    parts = []
    summary = clean_text(schema.get('description'))
    if summary:
        parts.append(summary)
    for container in soup.select('article [data-container="text"]'):
        text = clean_text(container)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def make_record(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return None

    title = clean_text(schema.get('name'))
    canonical_url = clean_text(schema.get('url')) or url
    location = schema.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country_code = clean_text(address.get('addressCountry')).upper()
    start = schema.get('startDate') or ''
    try:
        start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None

    if not city and venue:
        city = 'München'
    if not country_code:
        country_code = 'DE'
    if not title or not canonical_url or not venue or not city or country_code != 'DE':
        return None

    return {
        'title': title,
        'date': start_dt.date().isoformat(),
        'url': canonical_url,
        'time_from': start_dt.strftime('%H:%M') if 'T' in start else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from(soup, schema),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return make_record(url, response.text)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_record, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class GasteigDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gasteig_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        return get_concerts()


def main():
    GasteigDeCrawler().run()


if __name__ == '__main__':
    main()
