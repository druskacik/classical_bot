import re
from collections import deque
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ofp.pt/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda-1')
SOURCE = 'Orquestra Filarmónica Portuguesa'
LOCAL_TIMEZONE = ZoneInfo('Europe/Lisbon')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def clean_html(value):
    if not value:
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for unwanted in soup.select('script, style, nav, .sqs-block-button'):
        unwanted.decompose()
    text = soup.get_text('\n', strip=True)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def location_fields(item):
    location = item.get('location') or {}
    venue = (location.get('addressTitle') or '').strip()
    address_line = (location.get('addressLine1') or '').strip()

    # Squarespace sometimes stores a venue name in addressLine1. Do not use
    # actual street addresses as venue placeholders.
    looks_like_address = re.search(
        r'(^\d+\b|\b(?:rua|avenida|av\.|travessa|praça|largo|estrada)\b)',
        address_line,
        flags=re.IGNORECASE,
    )
    if not venue and address_line and not looks_like_address:
        venue = address_line

    locality = (location.get('addressLine2') or '').strip()
    city = locality.split(',', 1)[0].strip()
    city = re.sub(r'\s+\d{4}-\d{3}$', '', city).strip()
    return venue or None, city or None


def parse_item(item, description):
    title = (item.get('title') or '').strip()
    path = item.get('fullUrl')
    start_timestamp = item.get('startDate')
    venue, city = location_fields(item)

    try:
        start = datetime.fromtimestamp(start_timestamp / 1000, LOCAL_TIMEZONE)
    except (TypeError, ValueError, OSError):
        start = None

    url = urljoin(SOURCE_URL, path) if path else None
    if not all((title, url, start, venue, city)):
        log_message(
            'Skipping OFP event with missing required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url or AGENDA_URL,
            has_title=bool(title),
            has_date=bool(start),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'PT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OfpPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ofp_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='classical',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = build_session()
        response = session.get(AGENDA_URL, params={'format': 'json'}, timeout=45)
        response.raise_for_status()
        agenda = response.json()

        seeds = agenda.get('upcoming', []) + agenda.get('past', [])
        queue = deque(item.get('fullUrl') for item in seeds if item.get('fullUrl'))
        visited = set()
        records = []

        while queue:
            path = queue.popleft()
            if path in visited:
                continue
            visited.add(path)
            url = urljoin(SOURCE_URL, path)
            try:
                detail_response = session.get(url, params={'format': 'json'}, timeout=45)
                detail_response.raise_for_status()
                detail = detail_response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch OFP event detail',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            item = detail.get('item') or {}
            record = parse_item(item, clean_html(item.get('body')))
            if record:
                records.append(record)

            pagination = detail.get('pagination') or {}
            for key in ('prevItem', 'nextItem'):
                neighbour = (pagination.get(key) or {}).get('fullUrl')
                if neighbour and neighbour not in visited:
                    queue.append(neighbour)

        if not visited:
            raise ValueError('OFP agenda returned no event items')

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OfpPtCrawler().run()


if __name__ == '__main__':
    main()
