import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.flagstaffsymphony.org/'
SOURCE = 'Flagstaff Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
DEFAULT_CITY = 'Flagstaff'
DEFAULT_VENUE = 'NAU Ardrey Memorial Auditorium'

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
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(event):
    details = event.get('start_date_details') or {}
    try:
        event_date = datetime(
            int(details['year']), int(details['month']), int(details['day'])
        ).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return None, None

    time_from = None
    if not event.get('all_day'):
        try:
            time_from = f"{int(details['hour']):02d}:{int(details['minutes']):02d}"
        except (KeyError, TypeError, ValueError):
            pass
    return event_date, time_from


def infer_main_venue(title, description):
    """Infer FSO's main hall only for clearly billed orchestra performances."""
    text = f'{title} {description}'.lower()
    concert_evidence = (
        'flagstaff symphony orchestra',
        'full symphony orchestra',
        'the fso',
        'our concert',
        'symphonie fantastique',
    )
    excluded = ('auction', 'home tour', 'video format', 'golf tournament', '5k run')
    if any(term in text for term in concert_evidence) and not any(term in text for term in excluded):
        return DEFAULT_VENUE
    return ''


def venue_from_description(description):
    match = re.search(
        r'\b(?:take place|held|performed) at ([^\n.]+?)(?:\s+on\b|[.,]|$)',
        description,
        re.IGNORECASE,
    )
    return clean_text(match.group(1)) if match else ''


def event_to_record(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    event_date, time_from = parse_start(event)
    description = clean_text(event.get('description')) or None

    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city')) or (DEFAULT_CITY if venue else '')
    if re.match(r'^\d+\s', venue):
        venue = venue_from_description(description or '')
    if not venue:
        venue = infer_main_venue(title, description or '')
        city = DEFAULT_CITY if venue else ''

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
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'start_date': '2000-01-01',
                'status': 'publish',
                'per_page': 50,
                'page': page,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []
        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No valid events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class FlagstaffSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='flagstaffsymphony_org',
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
        return scrape_events()


def main():
    FlagstaffSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
