import json
import re
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://apollosfire.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
API_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
SOURCE = "Apollo's Fire"
CALENDAR_ID = '5802'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'united states': 'US',
    'usa': 'US',
    'us': 'US',
    'belgium': 'BE',
    'france': 'FR',
    'germany': 'DE',
    'portugal': 'PT',
    'united kingdom': 'GB',
    'uk': 'GB',
    'england': 'GB',
}

# A few otherwise-empty venue records on the site have unambiguous place names.
VENUE_CITIES = {
    'the bath church (ucc)': 'Bath',
    'the bath church': 'Bath',
}

CITY_CORRECTIONS = {
    'Daytona Bach': 'Daytona Beach',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', unescape(str(value)).replace('\xa0', ' ')).strip()


def calendar_events(session):
    start = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(datetime.now(timezone.utc).year + 4, 12, 31, 23, 59, 59,
                       tzinfo=timezone.utc).timestamp())
    response = session.post(
        API_URL,
        data={
            'action': 'vem_get_events',
            'id': CALENDAR_ID,
            'event': '0',
            'start': start,
            'end': end,
            'moment': int(datetime.now(timezone.utc).timestamp()),
            'futureOnly': 'false',
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get('events', [])


def event_url(value):
    return urljoin(SOURCE_URL, clean_text(value).replace('&#038;', '&'))


def country_code(address):
    country = clean_text(address.get('addressCountry')).lower()
    if country in COUNTRY_CODES:
        return COUNTRY_CODES[country]
    region = clean_text(address.get('addressRegion'))
    if re.fullmatch(r'[A-Z]{2}', region):
        return 'US'
    # Apollo's Fire is a US ensemble. Its domestic venue entries sometimes
    # omit addressCountry, while international tour entries name the country.
    if not country:
        return 'US'
    return None


def parse_detail(url, session):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    canonical_url = response.url
    soup = BeautifulSoup(response.text, 'html.parser')

    detail = soup.select_one('.vem-single-event-details')
    artists = soup.select_one('.vem-single-event-field-set.field-set-one')
    description_parts = []
    for node in (detail, artists):
        if node:
            text = clean_text(node.get_text(' ', strip=True))
            if text and text not in description_parts:
                description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        if data.get('@type') != 'Event':
            continue

        title = clean_text(data.get('name'))
        start_date = clean_text(data.get('startDate'))
        location = data.get('location') or {}
        address = location.get('address') or {}
        venue = clean_text(location.get('name'))
        city = clean_text(address.get('addressLocality'))
        city = CITY_CORRECTIONS.get(city, city)
        if not city:
            city = VENUE_CITIES.get(venue.lower(), '')
        code = country_code(address)
        if not code and city and venue.lower() in VENUE_CITIES:
            code = 'US'

        try:
            start = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            date = start.date().isoformat()
            time_from = start.strftime('%H:%M')
        except ValueError:
            continue
        if not title or not venue or not city or not code:
            continue

        records.append({
            'title': title,
            'date': date,
            'url': canonical_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = calendar_events(session)
    urls = sorted({event_url(item.get('url')) for item in events if item.get('url')})

    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(parse_detail, url, session): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Could not parse event detail',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No valid concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ApollosFireOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='apollosfire_org',
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
    ApollosFireOrgCrawler().run()


if __name__ == '__main__':
    main()
