import html
import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://csphilharmonic.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Colorado Springs Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': f'{SOURCE_URL}event/',
}

OPTION_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?\s+at\s+'
    r'(\d{1,2})(?::(\d{2}))?\s*([ap]m)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def get_response(session, url, *, params=None, accept='application/json'):
    last_error = None
    for attempt in range(4):
        try:
            response = session.get(
                url,
                params=params,
                headers={'Accept': accept},
                timeout=45,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise last_error


def fetch_events(session):
    today = date.today()
    params = {
        'per_page': 50,
        'start_date': '2000-01-01',
        'end_date': f'{today.year + 10}-12-31',
        'page': 1,
    }
    events = []
    while True:
        payload = get_response(session, EVENTS_API_URL, params=params).json()
        events.extend(payload.get('events', []))
        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1
    return events


def occurrence_from_option(value, event_start):
    match = OPTION_RE.search(clean_text(value))
    if not match:
        return None
    month, day, explicit_year, hour, minute, meridiem = match.groups()
    try:
        month_number = datetime.strptime(month, '%B').month
        start = datetime.strptime(event_start, '%Y-%m-%d %H:%M:%S')
        years = [int(explicit_year)] if explicit_year else [start.year - 1, start.year, start.year + 1]
        candidates = [datetime(year, month_number, int(day)) for year in years]
        event_date = min(candidates, key=lambda item: abs((item - start).days)).date().isoformat()
        hour_number = int(hour) % 12 + (12 if meridiem.lower() == 'pm' else 0)
        return event_date, f'{hour_number:02d}:{int(minute or 0):02d}'
    except ValueError:
        return None


def detail_data(session, event):
    response = get_response(session, event['url'], accept='text/html,application/xhtml+xml')
    soup = BeautifulSoup(response.text, 'html.parser')

    occurrences = []
    dates_select = soup.select_one('select.dates')
    if dates_select:
        for option in dates_select.find_all('option'):
            occurrence = occurrence_from_option(option.get_text(' ', strip=True), event['start_date'])
            if occurrence and occurrence not in occurrences:
                occurrences.append(occurrence)

    if not occurrences:
        try:
            start = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S')
            occurrences.append((start.date().isoformat(), start.strftime('%H:%M')))
        except (KeyError, ValueError):
            pass

    description_parts = []
    for selector in ('.programs', '.about_single_event'):
        node = soup.select_one(selector)
        text = clean_text(node)
        if text and text not in description_parts:
            description_parts.append(text)
    if not description_parts:
        fallback = clean_text(event.get('description'))
        if fallback:
            description_parts.append(fallback)

    return occurrences, '\n\n'.join(description_parts) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for event in fetch_events(session):
        title = clean_text(event.get('title'))
        url = event.get('url')
        venue_data = event.get('venue') or {}
        venue = clean_text(venue_data.get('venue'))
        city = clean_text(venue_data.get('city'))
        if not all((title, url, venue, city, event.get('start_date'))):
            log_message(
                'Skipping event with incomplete required data',
                event='crawler_event_skipped',
                level='warning',
                url=url,
                error_type='IncompleteEventData',
            )
            continue

        try:
            occurrences, description = detail_data(session, event)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Event detail request failed; using API event data',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            try:
                start = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S')
                occurrences = [(start.date().isoformat(), start.strftime('%H:%M'))]
            except ValueError:
                occurrences = []
            description = clean_text(event.get('description')) or None

        for event_date, time_from in occurrences:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class CsphilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='csphilharmonic_org',
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
    CsphilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
