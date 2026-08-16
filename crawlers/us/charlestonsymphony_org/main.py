import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://charlestonsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts/')
SOURCE = 'Charleston Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def flight_values(html):
    """Decode string values embedded in the Next.js React Flight response."""
    values = []
    for script in BeautifulSoup(html, 'html.parser').find_all('script'):
        match = re.fullmatch(
            r'self\.__next_f\.push\((.*)\)', script.string or '', re.DOTALL
        )
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except (TypeError, json.JSONDecodeError):
            continue
        if len(payload) > 1 and isinstance(payload[1], str):
            values.append(payload[1])
    return values


def listing_concerts(html):
    decoder = json.JSONDecoder()
    for value in flight_values(html):
        marker = '"concerts":'
        index = value.find(marker)
        if index < 0:
            continue
        try:
            concerts, _ = decoder.raw_decode(value[index + len(marker):])
        except json.JSONDecodeError:
            continue
        if isinstance(concerts, list):
            return resolve_flight_references(concerts)
    return []


def resolve_flight_references(concerts):
    reference_re = re.compile(r'^\$.*:concerts:(\d+):(.+)$')

    def resolve(value):
        if isinstance(value, str):
            match = reference_re.match(value)
            if not match:
                return value
            target = concerts[int(match.group(1))]
            for key in match.group(2).split(':'):
                target = target[int(key)] if isinstance(target, list) else target.get(key)
                if target is None:
                    break
            return resolve(target)
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        return value

    return [resolve(concert) for concert in concerts]


def parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'T(\d{2}):(\d{2})', str(value or ''))
    return f'{match.group(1)}:{match.group(2)}' if match else None


def occurrences(concert):
    times = concert.get('concertTimes') or {}
    values = [item for item in times.get('dates') or [] if isinstance(item, dict)]
    start = parse_date(concert.get('start'))
    first = parse_date(values[0].get('date')) if values else None
    # Older Craft records serialize local midnight as 20:00 on the previous
    # date. The explicit event start is the public calendar's displayed date.
    offset = start - first if start and first else timedelta()
    parsed = []
    for item in values:
        event_date = parse_date(item.get('date'))
        if event_date:
            parsed.append((event_date + offset, parse_time(item.get('time'))))
    if not parsed and start:
        parsed.append((start, parse_time(times.get('time'))))
    return parsed


def detail_description(session, url, fallback):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to scrape concert detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return clean_text(fallback) or None

    parts = []
    fallback_text = clean_text(fallback)
    if fallback_text:
        parts.append(fallback_text)
    for value in flight_values(response.text):
        if value.lstrip().startswith('<'):
            text = clean_text(value)
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def make_records(concert, description):
    title = clean_text(concert.get('title'))
    relative_url = concert.get('url')
    url = urljoin(SOURCE_URL, relative_url or '')
    venue_data = concert.get('venue') or {}
    address = venue_data.get('address') or {}
    venue = clean_text(venue_data.get('title'))
    city = clean_text(address.get('city'))
    if not city and 'daniel island' in title.lower():
        city = 'Daniel Island'
    elif not city and 'summerville' in title.lower():
        city = 'Summerville'
    if not title or not relative_url or not venue or not city:
        return []

    records = []
    for event_date, time_from in occurrences(concert):
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    concerts = listing_concerts(response.text)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                detail_description,
                session,
                urljoin(SOURCE_URL, concert.get('url') or ''),
                concert.get('descriptor'),
            ): concert
            for concert in concerts
        }
        for future in as_completed(futures):
            concert = futures[future]
            records.extend(make_records(concert, future.result()))

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class CharlestonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='charlestonsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CharlestonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
