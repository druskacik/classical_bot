import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mobilesymphony.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Mobile Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def clean_text(value):
    if not value:
        return ''
    value = html.unescape(str(value)).replace('\xa0', ' ')
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_api_description(value):
    # The Events Calendar API returns Divi shortcodes around otherwise useful text.
    value = html.unescape(value or '')
    value = re.sub(r'\[/?et_pb_[^\]]*\]', '\n', value)
    return clean_text(value) or None


def page_description(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    parts = []

    # Current event pages keep their synopsis and programme in these two Divi
    # template regions. Removing the event article avoids repeating metadata.
    for selector in ('.et_pb_section_1_tb_body', '.et_pb_row_2_tb_body'):
        node = soup.select_one(selector)
        if not node:
            continue
        for unwanted in node.select('script, style, form, article'):
            unwanted.decompose()
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def fetch_events(session):
    params = {
        'per_page': 50,
        'start_date': '2000-01-01 00:00:00',
        'end_date': '2100-12-31 23:59:59',
        'status': 'publish',
    }
    events = []
    page = 1
    while True:
        response = session.get(EVENTS_API_URL, params={**params, 'page': page}, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('events', []))
        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1
    return events


def fetch_descriptions(events):
    descriptions = {}

    def fetch(event):
        url = event.get('url')
        if not url:
            return event.get('id'), None
        session = make_session()
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return event.get('id'), page_description(response.text)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                event_id, description = future.result()
                descriptions[event_id] = description
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed; using API description',
                    event='crawler_detail_request_failed',
                    level='warning',
                    url=event.get('url'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return descriptions


def occurrence_datetimes(event):
    try:
        start = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S')
        end = datetime.strptime(event['end_date'], '%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError):
        return []

    all_day = bool(event.get('all_day'))
    values = [(start.date().isoformat(), None if all_day else start.strftime('%H:%M'))]
    if end.date() != start.date():
        end_time = None if all_day or end.strftime('%H:%M:%S') == '23:59:59' else end.strftime('%H:%M')
        values.append((end.date().isoformat(), end_time))
    return values


def event_records(event, description):
    title = clean_text(event.get('title'))
    url = (event.get('url') or '').rstrip('/') + '/'
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return []
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    if not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        return []

    description = description or clean_api_description(event.get('description'))
    return [
        {
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for date, time_from in occurrence_datetimes(event)
    ]


def scrape_concerts(session=None):
    session = session or make_session()
    events = fetch_events(session)
    descriptions = fetch_descriptions(events)
    records = []
    for event in events:
        records.extend(event_records(event, descriptions.get(event.get('id'))))

    if not records:
        log_message(
            'No valid events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MobileSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mobilesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    MobileSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
