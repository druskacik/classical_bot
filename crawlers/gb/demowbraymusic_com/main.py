import json
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.demowbraymusic.com/'
EVENT_SITEMAP_URL = f'{SOURCE_URL}event-pages-sitemap.xml'
SOURCE = 'De Mowbray Music'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The unfiltered Wix event sitemap also contains a multi-event ticket and an
# explicitly jazz-only performance. Neither is a candidate classical concert.
EXCLUDED_PATHS = {
    '/event-details/de-mowbray-music-weekend-ticket',
    '/event-details/de-mowbray-music-festival-an-evening-of-jazz',
}

UK_POSTCODE_RE = re.compile(
    r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    soup = BeautifulSoup(get_response(session, EVENT_SITEMAP_URL).content, 'xml')
    urls = []
    for node in soup.select('url > loc'):
        url = clean_text(node)
        path = urlparse(url).path.rstrip('/')
        if path.startswith('/event-details/') and path not in EXCLUDED_PATHS:
            urls.append(url)
    return list(dict.fromkeys(urls))


def event_json(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def extract_city(address):
    address = clean_text(address)
    if not address:
        return None
    parts = [part.strip() for part in address.split(',') if part.strip()]
    for part in reversed(parts):
        if part.upper() in {'UK', 'UNITED KINGDOM'}:
            continue
        without_postcode = UK_POSTCODE_RE.sub('', part).strip(' ,')
        without_number = re.sub(r'^\d+[A-Z]?\s+', '', without_postcode, flags=re.IGNORECASE)
        if without_number:
            return without_number
    return None


def parse_event(content, url):
    data = event_json(BeautifulSoup(content, 'html.parser'))
    if not data:
        return None

    title = clean_text(data.get('name'))
    start = clean_text(data.get('startDate'))
    location = data.get('location') if isinstance(data.get('location'), dict) else {}
    venue = clean_text(location.get('name'))
    city = extract_city(location.get('address'))
    if not title or not start or not venue or not city or venue.casefold() == city.casefold():
        return None

    try:
        occurrence = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except ValueError:
        return None

    return {
        'title': title,
        'date': occurrence.date().isoformat(),
        'url': url,
        'time_from': occurrence.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': clean_text(data.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class DemowbraymusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='demowbraymusic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        records = []
        for url in event_urls(session):
            try:
                record = parse_event(get_response(session, url).content, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape De Mowbray Music event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    DemowbraymusicComCrawler().run()


if __name__ == '__main__':
    main()
