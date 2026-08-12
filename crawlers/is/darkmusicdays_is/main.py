import html
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.darkmusicdays.is/'
CALENDAR_URL = urljoin(SOURCE_URL, 'eventscalendar')
SOURCE = 'Dark Music Days'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9,is;q=0.8',
}

VENUE_PATTERNS = (
    (r'\bKaldal[oó]n\b', 'Kaldalón'),
    (r'\bNorr[æa]na h[uú]si[ðd]\b|\bNordic House\b', 'The Nordic House'),
    (r'\bHallgr[ií]mskirkja\b', 'Hallgrímskirkja'),
    (r'\bHarpa(?: Concert Hall)?\b', 'Harpa'),
    (r'\bSalurinn(?:,? K[oó]pavogi)?\b', 'Salurinn'),
    (r'\bS[oö]ngsk[oó]linn [ií] Reykjav[ií]k\b', 'Söngskólinn í Reykjavík'),
    (r'\bGlerh[uú]si[ðd]\b', 'Glerhúsið'),
    (r'\b[ÁA]smundarsafn\b', 'Ásmundarsafn'),
    (r'\bGr[oó]farh[uú]s\b', 'Grófarhús'),
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_calendar_page(session, params):
    response = session.get(CALENDAR_URL, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def discover_items(session):
    params = {'format': 'json'}
    seen_ids = set()
    items = []

    while True:
        payload = fetch_calendar_page(session, params)
        for item in [*payload.get('upcoming', []), *payload.get('past', [])]:
            item_id = item.get('id')
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            items.append(item)

        pagination = payload.get('pagination') or {}
        if not pagination.get('nextPage'):
            break
        offset = pagination.get('nextPageOffset')
        if offset is None:
            log_message(
                'Calendar pagination did not provide an offset',
                event='crawler_pagination_invalid',
                level='warning',
                url=CALENDAR_URL,
            )
            break
        params = {'format': 'json', 'offset': offset}

    return items


def infer_venue(location, description):
    venue = clean_text((location or {}).get('addressTitle'))
    if venue:
        return venue
    for pattern, name in VENUE_PATTERNS:
        if re.search(pattern, description, re.IGNORECASE):
            return name
    return None


def infer_city(location, venue):
    location_text = clean_text(
        '\n'.join(
            str((location or {}).get(key) or '')
            for key in ('addressLine1', 'addressLine2', 'addressCountry')
        )
    )
    combined = f'{venue}\n{location_text}'.casefold()
    if 'kópavog' in combined or 'kopavog' in combined or 'salurinn' in combined:
        return 'Kópavogur'
    if 'reykjav' in combined:
        return 'Reykjavík'

    # The festival calendar is based in Reykjavík; its only observed event
    # outside the city is Salurinn in neighbouring Kópavogur, handled above.
    return 'Reykjavík'


def parse_item(item):
    title = clean_text(item.get('title'))
    url_path = item.get('fullUrl')
    start_timestamp = item.get('startDate')
    if not title or not url_path or not isinstance(start_timestamp, (int, float)):
        return None

    try:
        start = datetime.fromtimestamp(start_timestamp / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None

    end_timestamp = item.get('endDate')
    is_multi_day = (
        isinstance(end_timestamp, (int, float))
        and end_timestamp - start_timestamp >= 24 * 60 * 60 * 1000
    )

    description_parts = [
        clean_text(item.get('excerpt')),
        clean_text(item.get('body')),
    ]
    description = '\n\n'.join(
        part for index, part in enumerate(description_parts)
        if part and part not in description_parts[:index]
    ) or None
    location = item.get('location') or {}
    venue = infer_venue(location, description or '')
    if not venue:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, url_path),
        # Squarespace assigns nominal timestamps to multi-day installations,
        # although the public calendar deliberately displays no start time.
        'time_from': None if is_multi_day else start.strftime('%H:%M'),
        'venue': venue,
        'city': infer_city(location, venue),
        'country_code': 'IS',
        'description': description,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = discover_items(session)
    records = []
    for item in items:
        record = parse_item(item)
        if record:
            records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class DarkmusicdaysIsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='darkmusicdays_is',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IS',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    DarkmusicdaysIsCrawler().run()


if __name__ == '__main__':
    main()
