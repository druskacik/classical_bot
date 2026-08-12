import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://teatripiacenza.it/'
EVENTS_API = urljoin(SOURCE_URL, 'wp-json/tribe/events/v1/events')
SOURCE = 'Fondazione Teatri di Piacenza'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# The API does not expose addresses for these venue records. These mappings
# cover every explicitly named venue currently present in the full archive.
VENUE_CITIES = {
    'teatro municipale': 'Piacenza',
    'sala dei teatini': 'Piacenza',
    'ridotto del teatro municipale': 'Piacenza',
    'foyer del teatro municipale': 'Piacenza',
    'croara country club': 'Gazzola',
    'castello di agazzano': 'Agazzano',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def listing_events(session):
    # A wide explicit range returns the complete retained archive as well as
    # announced future seasons. Follow the API's pagination URLs verbatim.
    url = EVENTS_API
    params = {
        'per_page': 50,
        'start_date': '1900-01-01',
        'end_date': '2100-12-31',
    }
    events = []
    while url:
        payload = get_json(session, url, params=params)
        events.extend(payload.get('events') or [])
        url = payload.get('next_rest_url')
        params = None
    return events


def event_location(event):
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = VENUE_CITIES.get(venue.casefold())
    if not venue or not city:
        return None, None
    return venue, city


def detail_description(url, fallback=None):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"][href]')
    body = soup.select_one('.event-box.event-description')
    if body:
        for node in body.select('table, script, style, noscript'):
            node.decompose()
        description = clean_text(body)
    else:
        description = ''
    return (
        description or clean_text(fallback) or None,
        canonical.get('href') if canonical else response.url,
    )


def make_record(event):
    title = clean_text(event.get('title'))
    start = event.get('start_date') or ''
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}', start)
    url = (event.get('url') or '').strip()
    venue, city = event_location(event)
    if not title or not match or not url or not venue or not city:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': clean_text(event.get('description') or event.get('excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = [record for event in listing_events(session) if (record := make_record(event))]

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, record['url'], record['description']): record
            for record in records
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'], record['url'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class TeatriPiacenzaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatripiacenza_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
        return get_concerts()


def main():
    TeatriPiacenzaItCrawler().run()


if __name__ == '__main__':
    main()
