import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestraoftheswan.org/'
SOURCE = 'Orchestra of the Swan'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, params):
    response = session.get(API_URL, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def location_from_description(description_html):
    soup = BeautifulSoup(description_html or '', 'html.parser')
    for heading in soup.select('h1, h2, h3, h4, h5, h6, strong'):
        if clean_text(heading).rstrip(':').lower() != 'location':
            continue
        node = heading.find_next(['p', 'div'])
        location = clean_text(node).split('\n', 1)[0] if node else ''
        if location:
            return location
    return ''


def parse_location(event):
    venue_data = event.get('venue') or {}
    if isinstance(venue_data, list):
        venue_data = venue_data[0] if venue_data else {}

    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    if venue and city:
        return venue, city

    location = location_from_description(event.get('description'))
    parts = [part.strip() for part in location.split(',') if part.strip()]
    if not venue and parts:
        venue = parts[0]
    if not city and len(parts) > 1:
        city = parts[1]
    return venue, city


def concert_time(description, fallback):
    text = clean_text(BeautifulSoup(description or '', 'html.parser'))
    match = re.search(
        r'(?:for\s+|starting\s+at\s+)(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\s+concert',
        text,
        re.IGNORECASE,
    )
    if not match:
        return fallback
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour not in range(1, 13) or minute > 59:
        return fallback
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = clean_text(event.get('start_date'))
    venue, city = parse_location(event)
    if not title or not url or not start or not venue or not city:
        return None

    try:
        event_date, event_time = start.split(' ', 1)
        date.fromisoformat(event_date)
    except (ValueError, TypeError):
        return None

    description_html = event.get('description') or ''
    description = clean_text(BeautifulSoup(description_html, 'html.parser')) or None
    time_from = None if event.get('all_day') else concert_time(description_html, event_time[:5])
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    page = 1
    records = []

    while True:
        payload = get_json(
            session,
            {
                'page': page,
                'per_page': 50,
                'start_date': '2000-01-01',
                'end_date': '2100-12-31',
                'status': 'publish',
            },
        )
        for event in payload.get('events', []):
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped Orchestra of the Swan event with incomplete details',
                    event='crawler_item_skipped',
                    level='warning',
                    url=clean_text(event.get('url')),
                )

        if page >= int(payload.get('total_pages') or 1):
            break
        page += 1

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class OrchestraOfTheSwanOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestraoftheswan_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        return get_concerts()


def main():
    OrchestraOfTheSwanOrgCrawler().run()


if __name__ == '__main__':
    main()
