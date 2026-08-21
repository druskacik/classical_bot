import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.judithshatin.com/'
SOURCE = 'Judith Shatin'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
PAGE_SIZE = 50

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

COUNTRY_CODES = {
    'france': 'FR',
    'germany': 'DE',
    'ireland': 'IE',
    'israel': 'IL',
    'mexico': 'MX',
    'spain': 'ES',
    'switzerland': 'CH',
    'turkey': 'TR',
    'türkiye': 'TR',
    'united states': 'US',
    'united states of america': 'US',
    'usa': 'US',
}

US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'\[/?[A-Za-z][^\]]*\]', ' ', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def country_code_for(venue):
    country = clean_text(venue.get('country')).casefold().rstrip('.')
    code = COUNTRY_CODES.get(country)
    if code:
        return code

    # Many older US venues omit country but retain a two-letter state.
    state = clean_text(venue.get('state') or venue.get('province')).upper().rstrip('.')
    if state in US_STATES:
        return 'US'
    return None


def description_for(event):
    parts = []
    for field in ('description', 'excerpt'):
        text = clean_text(event.get(field))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def record_from_event(event):
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None

    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city')).strip(' ,')
    country_code = country_code_for(venue_data)

    try:
        start = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue or not city or not country_code:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_for(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_page(session, page):
    response = session.get(
        API_URL,
        params={
            'per_page': PAGE_SIZE,
            'page': page,
            'start_date': '1900-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    skipped_count = 0
    page = 1

    while True:
        payload = fetch_page(session, page)
        events = payload.get('events') or []
        for event in events:
            record = record_from_event(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        total_pages = int(payload.get('total_pages') or 1)
        if not events or page >= total_pages:
            break
        page += 1

    if skipped_count:
        log_message(
            'Skipped Judith Shatin events without a complete date and location',
            event='crawler_records_skipped',
            level='warning',
            url=API_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No parseable Judith Shatin events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return records


class JudithshatinComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='judithshatin_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        return scrape_concerts()


def main():
    JudithshatinComCrawler().run()


if __name__ == '__main__':
    main()
