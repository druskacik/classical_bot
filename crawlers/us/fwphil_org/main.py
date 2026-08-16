import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fwphil.org/'
SOURCE = 'Fort Wayne Philharmonic'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': 'Googlebot',
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    for break_node in soup.find_all('br'):
        break_node.replace_with('\n')
    for block in soup.find_all(['p', 'div', 'li', 'h1', 'h2', 'h3', 'h4']):
        block.append('\n')
    text = soup.get_text(' ', strip=False).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    try:
        starts_at = datetime.fromisoformat(event.get('start_date', ''))
        event_date = starts_at.date().isoformat()
        time_from = None if event.get('all_day') else starts_at.strftime('%H:%M')
    except (TypeError, ValueError):
        return None

    if not all((title, url, venue, city)) or not url.startswith(('http://', 'https://')):
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
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    params = {
        'per_page': 50,
        'page': 1,
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
        'status': 'publish',
    }
    records = []

    while True:
        try:
            response = session.get(EVENTS_API_URL, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Fort Wayne Philharmonic events API request failed',
                event='crawler_request_failed',
                level='error',
                url=EVENTS_API_URL,
                page=params['page'],
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        events = payload.get('events') or []
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_record_skipped',
                    level='warning',
                    url=event.get('url') or EVENTS_API_URL,
                    event_id=event.get('id'),
                )

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1

    if not records:
        log_message(
            'No Fort Wayne Philharmonic event records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class FwphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fwphil_org',
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
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    FwphilOrgCrawler().run()


if __name__ == '__main__':
    main()
