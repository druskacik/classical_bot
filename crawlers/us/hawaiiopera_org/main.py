import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hawaiiopera.org/'
CALENDAR_URL = f'{SOURCE_URL}calendar'
SOURCE = "Hawai'i Opera Theatre"
API_URL = (
    'https://inffuse.eventscalendar.co/api/v0.1/projects/'
    'proj_zOiQ3obwdoe7CDdE3VMGb/data/public/events'
)
API_PARAMS = {
    'user': 'user_rlNAfuurUH2LLsIutYH8g',
    'app': 'calendar',
}

# The public calendar currently duplicates this occurrence on April 30, while
# HOT's Mainstage page lists the 4 p.m. performance on May 2.
DATE_CORRECTIONS = {
    'event_5frJKTFuoADa58uPmz4Cf': '2027-05-02',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Referer': CALENDAR_URL,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def valid_date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return ''


def event_url(event):
    for button in event.get('buttons') or []:
        url = button.get('url')
        if isinstance(url, str) and url.startswith(('https://', 'http://')):
            return url
    return CALENDAR_URL


def venue_and_city(event):
    title = clean_text(event.get('title')).lower()
    location = clean_text(event.get('location'))
    description = clean_text(event.get('description'))
    evidence = f'{location}\n{description}'.lower()

    if '777 ward' in evidence:
        return 'Blaisdell Concert Hall', 'Honolulu'
    if 'studio101' in evidence or 'studio 101' in evidence:
        return 'STUDIO101', 'Honolulu'
    if 'moana surfrider' in evidence:
        return 'Moana Surfrider, Diamond Lawn & Terrace', 'Honolulu'
    if '848 s. beretania' in evidence and (
        'resale shop' in title or 'shopping event' in title or 'suite 301' in evidence
    ):
        return 'ACT II Resale Shop', 'Honolulu'

    # Retain a named API location, but never turn a bare street address into a venue.
    if location and not re.match(r'^\d+\s', location):
        city = 'Honolulu' if 'honolulu' in evidence else ''
        return location, city
    return '', ''


def parse_event(event, sibling_venues=None):
    title = clean_text(event.get('title'))
    event_date = valid_date(DATE_CORRECTIONS.get(event.get('id'), event.get('startDate')))
    venue, city = venue_and_city(event)
    if not venue and sibling_venues:
        sibling_key = clean_text(event.get('description'))
        venue, city = sibling_venues.get(sibling_key, ('', ''))
    if not all((title, event_date, venue, city)):
        return None

    hour = event.get('startHour')
    minute = event.get('startMinutes')
    time_from = None
    if isinstance(hour, int) and isinstance(minute, int) and 0 <= hour <= 23 and 0 <= minute <= 59:
        time_from = f'{hour:02d}:{minute:02d}'

    return {
        'title': re.sub(r'\s+-\s+#\d+$', '', title),
        'date': event_date,
        'url': event_url(event),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(API_URL, params=API_PARAMS, headers=HEADERS, timeout=45)
    response.raise_for_status()
    payload = response.json()
    events = payload.get('value') if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise ValueError('EventsCalendar API returned an unexpected response')

    records = []
    skipped_count = 0
    sibling_venues = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        venue, city = venue_and_city(event)
        description = clean_text(event.get('description'))
        if venue and city and description:
            sibling_venues[description] = (venue, city)

    for event in events:
        record = parse_event(event, sibling_venues) if isinstance(event, dict) else None
        if record:
            records.append(record)
        else:
            skipped_count += 1

    log_message(
        'Calendar API scrape completed',
        event='crawler_api_scrape_completed',
        url=API_URL,
        record_count=len(records),
        skipped_count=skipped_count,
    )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class HawaiiOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hawaiiopera_org',
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
    HawaiiOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
