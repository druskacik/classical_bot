import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hfmt-hamburg.de/'
EVENTS_URL = urljoin(SOURCE_URL, 'hochschule/aktuelles/veranstaltungen')
SOURCE = 'Hochschule für Musik und Theater Hamburg'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    soup = fetch(session, EVENTS_URL)
    urls = set()
    for card in soup.select('#events [data-event][data-date]'):
        link = card.select_one('h5.card-title a[href]')
        if link:
            urls.add(urljoin(SOURCE_URL, link['href']))
    return sorted(urls)


def event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def description_text(soup):
    container = soup.select_one('.frame-type-hfmtdb_event .mw-text')
    if not container:
        return None

    parts = []
    subtitle = container.select_one('h1 + .fs-3')
    if subtitle:
        parts.append(clean_text(subtitle))
    for paragraph in container.select('p'):
        text = clean_text(paragraph)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail(soup, url):
    data = event_data(soup)
    title = clean_text(data.get('name'))
    starts_at = data.get('startDate') or ''
    location = data.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    if not title or not starts_at or not venue or not city:
        return None

    try:
        start = datetime.fromisoformat(starts_at)
    except ValueError:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M') if 'T' in starts_at else None,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description_text(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return parse_detail(fetch(session, url), url)


class HfmtHamburgDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfmt_hamburg_de',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = listing_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(scrape_detail, url): url for url in urls}
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
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    HfmtHamburgDeCrawler().run()


if __name__ == '__main__':
    main()
