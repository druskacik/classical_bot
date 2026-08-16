import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nmphil.org/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'New Mexico Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

# The public archive currently begins in 2022. A broad fixed lower bound keeps
# all archives and a rolling upper bound also captures announced future seasons.
ARCHIVE_START = '2000-01-01'
INVALID_VENUES = {'location abq', 'albuquerque'}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    if '<' in raw:
        soup = BeautifulSoup(raw, 'html.parser')
        for element in soup.select('script, style, noscript'):
            element.decompose()
        text = soup.get_text('\n', strip=True)
    else:
        text = raw
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(event):
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
        hour = int(details['hour'])
        minute = int(details['minutes'])
    except (KeyError, TypeError, ValueError):
        return event_date, None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return event_date, None
    return event_date, f'{hour:02d}:{minute:02d}'


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    event_date, time_from = parse_start(event)
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    # Older entries sometimes use “Location ABQ” or the city itself in the
    # venue field. Those are placeholders, not defensible venue names.
    if venue.lower() in INVALID_VENUES or venue.lower() == city.lower():
        venue = ''
    country = clean_text(venue_data.get('country')).lower()
    if country and country not in {'united states', 'united states of america', 'usa', 'us'}:
        return None
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


def get_events(session):
    end_year = date.today().year + 10
    params = {
        'per_page': 50,
        'start_date': ARCHIVE_START,
        'end_date': f'{end_year}-12-31',
        'page': 1,
    }
    events = []
    while True:
        response = session.get(API_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('events') or [])
        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1
    return events


class NmPhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nmphil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = get_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch New Mexico Philharmonic events API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        skipped = 0
        for event in events:
            record = make_record(event)
            if record:
                records.append(record)
            else:
                skipped += 1
        if skipped:
            log_message(
                'Skipped New Mexico Philharmonic entries with invalid location data',
                event='crawler_items_skipped',
                level='warning',
                record_count=skipped,
                url=API_URL,
                error_type='IncompleteEventData',
                error_message='Required title, date, URL, venue, or city was unavailable',
            )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    NmPhilOrgCrawler().run()


if __name__ == '__main__':
    main()
