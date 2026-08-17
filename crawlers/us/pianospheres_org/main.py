import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pianospheres.org/'
SOURCE = 'Piano Spheres'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': f'{SOURCE_URL}events/',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    try:
        start = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return '', None
    return start.date().isoformat(), start.strftime('%H:%M')


def normalize_city(value):
    city = clean_text(value)
    return re.sub(r',\s*[A-Z]{2}$', '', city).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    event_date, time_from = parse_start(event.get('start_date'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = normalize_city(venue_data.get('city'))

    # Two archive entries identify their physical location only in the venue
    # name. This is specific enough to retain without applying a blanket home-
    # city default, which would be wrong for Piano Spheres' touring concerts.
    if not city and venue.casefold() == 'a private home in pasadena':
        city = 'Pasadena'

    # Old streaming listings used this instruction as a venue. It is neither a
    # physical venue nor enough evidence for a city, so those entries are skipped.
    if venue.casefold() == 'click the link below':
        venue = ''

    if not all((title, event_date, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class PianospheresOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pianospheres_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        page = 1
        events = []

        while True:
            response = session.get(
                EVENTS_API_URL,
                params={
                    'start_date': '1900-01-01 00:00:00',
                    'end_date': '2100-12-31 23:59:59',
                    'per_page': 50,
                    'page': page,
                },
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            events.extend(payload.get('events') or [])
            if page >= int(payload.get('total_pages') or 1):
                break
            page += 1

        records = []
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Piano Spheres event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=clean_text(event.get('url')) or SOURCE_URL,
                    error_type='IncompleteEventData',
                    error_message='Required date, title, URL, venue, or city is missing',
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    PianospheresOrgCrawler().run()


if __name__ == '__main__':
    main()
