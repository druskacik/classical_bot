import html
import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fontanamix.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/ajde_events'
SOURCE = 'FontanaMIX ensemble'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_city(address, description):
    if isinstance(address, dict):
        locality = clean_text(address.get('addressLocality'))
        if locality:
            return locality
        address = address.get('streetAddress') or ''
    address = clean_text(address)

    for name in ('Bologna', 'Budrio', 'Bilbao'):
        if re.search(rf'\b{re.escape(name)}\b', address, re.I):
            return name

    postal_match = re.search(r'\b\d{5}\s+(.+?)(?:\s+Italia)?$', address, re.I)
    if postal_match:
        city = postal_match.group(1).strip(' ,')
        city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', city)
        if city:
            return city

    # FontanaMIX's own calendar is based in Bologna. Touring locations expose
    # their city in EventON's address (for example Bilbao), so only use the
    # organization's strong home-city default when the address omits a city.
    return 'Bologna'


def parse_record(schema, fallback_url):
    title = clean_text(schema.get('name'))
    url = clean_text(schema.get('url')) or fallback_url
    start = clean_text(schema.get('startDate'))
    if not title or not url or not start:
        return None
    match = re.fullmatch(
        r'(\d{4})-(\d{1,2})-(\d{1,2})(?:T(\d{1,2}):(\d{2})(?::\d{2})?(?:Z|[+-]\d{1,2}:\d{2})?)?',
        start,
    )
    if not match:
        return None
    try:
        start_at = datetime(*[int(value) for value in match.groups('0')[:5]])
    except ValueError:
        return None
    event_date = start_at.date().isoformat()
    time_from = start_at.strftime('%H:%M') if match.group(4) is not None else None

    location = schema.get('location')
    if isinstance(location, list):
        location = next((item for item in location if isinstance(item, dict)), None)
    if not isinstance(location, dict):
        return None
    venue = clean_text(location.get('name'))

    description_html = schema.get('description') or ''
    description = clean_text(BeautifulSoup(description_html, 'html.parser'))
    city = parse_city(location.get('address'), description)
    if not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FontanamixItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fontanamix_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(API_URL, params={'per_page': 100, 'page': 1}, timeout=45)
            response.raise_for_status()
            events = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch FontanaMIX event feed',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for event in events:
            url = event.get('link')
            if not url:
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                schema = event_schema(BeautifulSoup(response.content, 'html.parser'))
                record = parse_record(schema, url) if schema else None
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch FontanaMIX event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FontanamixItCrawler().run()


if __name__ == '__main__':
    main()
