import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.svmusicfestival.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Sun Valley Music Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_inline(value):
    return re.sub(r'\s+', ' ', clean_text(value)).strip()


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def parse_event(item):
    start = parse_datetime(item.get('start_date'))
    end = parse_datetime(item.get('end_date'))
    venue_data = item.get('venue')
    venue_data = venue_data if isinstance(venue_data, dict) else {}

    title = clean_inline(item.get('title'))
    url = (item.get('url') or '').strip()
    venue = clean_inline(venue_data.get('venue'))
    city = clean_inline(venue_data.get('city'))

    # This recurring student concert's API entries omit their venue object. Its
    # title, the site's calendar presentation, and adjacent editions explicitly
    # identify it as the Pavilion performance in Sun Valley.
    if title == 'Afternoon Pavilion Concert' and not venue and not city:
        venue = 'Sun Valley Pavilion'
        city = 'Sun Valley'

    if not title or not start or not url or not venue or not city:
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url or EVENTS_API_URL,
            has_title=bool(title),
            has_date=bool(start),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    all_day = bool(item.get('all_day'))
    description = clean_text(item.get('description')) or clean_text(item.get('excerpt'))

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if all_day else start.strftime('%H:%M'),
        'time_to': None if all_day or not end else end.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    page = 1
    records = []

    while True:
        params = {
            'per_page': 50,
            'page': page,
            'start_date': '1900-01-01',
            'end_date': f'{datetime.now().year + 10}-12-31',
        }
        response = session.get(EVENTS_API_URL, params=params, headers=HEADERS, timeout=45)
        response.raise_for_status()
        payload = response.json()

        events = payload.get('events') or []
        for item in events:
            record = parse_event(item)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No candidate events found in events feed',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class SvMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='svmusicfestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'time_to',
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
    SvMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
