import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ospc.fr/'
SOURCE = 'Orchestre Perpignan Catalogne'
EVENTS_API = f'{SOURCE_URL}index.php/wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fr-FR,fr;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value))
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def event_description(value):
    if not value:
        return None
    soup = BeautifulSoup(html.unescape(value), 'html.parser')
    for selector in (
        '.tribe-events-schedule',
        '.tribe-block__event-price',
        '.tribe-block__venue',
        '.tribe-block__events-link',
    ):
        for node in soup.select(selector):
            node.decompose()
    description = clean_text(soup)
    return description or None


def api_events(session):
    records = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API,
            params={
                'page': page,
                'per_page': 50,
                'start_date': '1900-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
                'status': 'publish',
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []
        records.extend(events)
        total_pages = int(payload.get('total_pages') or 0)
        if page >= total_pages:
            return records
        page += 1


def occurrence(event):
    details = event.get('start_date_details') or {}
    try:
        event_date = date(
            int(details['year']), int(details['month']), int(details['day'])
        ).isoformat()
    except (KeyError, TypeError, ValueError):
        return None, None

    if event.get('all_day'):
        return event_date, None
    try:
        hour, minute = int(details['hour']), int(details['minutes'])
        time_from = f'{hour:02d}:{minute:02d}' if hour < 24 and minute < 60 else None
    except (KeyError, TypeError, ValueError):
        time_from = None
    return event_date, time_from


def event_place(event, description):
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    if venue and city and venue.casefold() != city.casefold():
        return venue, city

    # One published occurrence lacks a linked venue but states both the hall
    # and locality explicitly in its body. Do not infer a home venue for other
    # incomplete or touring records.
    match = re.search(
        r'\b(ESPACE CULTUREL\s+[«"]?\s*DANIEL TOSI\s*[»"]?)\s+'
        r'CARRER DEL REY\s+66300\s+LLUPIA\b',
        description or '',
        re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1)), 'Llupia'
    return None, None


def event_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    event_date, time_from = occurrence(event)
    description = event_description(event.get('description'))
    venue, city = event_place(event, description)
    if not all((title, url, event_date, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': description,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    try:
        events = api_events(session)
    except (requests.RequestException, ValueError, TypeError) as error:
        log_message(
            'Failed to fetch Orchestre Perpignan Catalogne events',
            event='crawler_fetch_failed',
            level='error',
            url=EVENTS_API,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = [record for event in events if (record := event_record(event))]
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class OspcFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ospc_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OspcFrCrawler().run()


if __name__ == '__main__':
    main()
