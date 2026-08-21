import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://konstantinlifschitz.com/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Konstantin Lifschitz'

COUNTRY_CODES = {
    'argentina': 'AR', 'australia': 'AU', 'austria': 'AT', 'belgium': 'BE',
    'brazil': 'BR', 'canada': 'CA', 'china': 'CN', 'croatia': 'HR',
    'czech republic': 'CZ', 'czechia': 'CZ', 'denmark': 'DK', 'estonia': 'EE',
    'finland': 'FI', 'france': 'FR', 'germany': 'DE', 'greece': 'GR',
    'hong kong': 'HK', 'hungary': 'HU', 'ireland': 'IE', 'israel': 'IL',
    'italy': 'IT', 'japan': 'JP', 'latvia': 'LV', 'lithuania': 'LT',
    'luxembourg': 'LU', 'netherlands': 'NL', 'new zealand': 'NZ',
    'norway': 'NO', 'poland': 'PL', 'portugal': 'PT', 'romania': 'RO',
    'russia': 'RU', 'singapore': 'SG', 'slovakia': 'SK', 'slovenia': 'SI',
    'south korea': 'KR', 'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH',
    'taiwan': 'TW', 'ukraine': 'UA', 'united arab emirates': 'AE',
    'united kingdom': 'GB', 'uk': 'GB', 'united states': 'US', 'usa': 'US',
}

EXACT_DATE_RE = re.compile(r'\((\d{1,2})[.]([01]?\d)(?:[.]((?:19|20)\d{2}))?\)')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n *', '\n', text).strip()


def event_date(event):
    try:
        start = datetime.fromisoformat(event['start_date'])
        end = datetime.fromisoformat(event['end_date'])
    except (KeyError, TypeError, ValueError):
        return None

    if event.get('all_day') and end.date() > start.date():
        # Multi-day engagements are often overview records. Keep one only when
        # its title identifies a concrete performance date within that range.
        match = EXACT_DATE_RE.search(clean_text(event.get('title')))
        if not match:
            return None
        year = int(match.group(3) or start.year)
        try:
            precise = start.replace(year=year, month=int(match.group(2)), day=int(match.group(1)))
        except ValueError:
            return None
        if not start.date() <= precise.date() <= end.date():
            return None
        return precise.date().isoformat()

    return start.date().isoformat()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = str(event.get('url') or '').strip()
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country_code = COUNTRY_CODES.get(clean_text(venue_data.get('country')).lower())
    date = event_date(event)

    # The API occasionally puts a festival name in the city field and the city
    # itself in the venue field. That does not establish a usable venue.
    malformed_location = 'festival' in city.lower() and venue.lower() in clean_text(
        venue_data.get('address')
    ).lower()
    if not all((title, date, url, venue, city, country_code)) or malformed_location:
        return None

    time_from = None
    if not event.get('all_day'):
        try:
            time_from = datetime.fromisoformat(event['start_date']).strftime('%H:%M')
        except (KeyError, TypeError, ValueError):
            pass

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_events(session):
    page = 1
    events = []
    while True:
        response = session.get(
            API_URL,
            params={
                'page': page,
                'per_page': 50,
                'start_date': '1900-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
                'status': 'publish',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get('events')
        if not isinstance(page_events, list):
            raise ValueError('Events API response does not contain an events list')
        events.extend(page_events)
        total_pages = int(payload.get('total_pages') or 0)
        if page >= total_pages:
            break
        page += 1
    return events


class KonstantinLifschitzComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='konstantinlifschitz_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({'User-Agent': 'classical-concert-crawler/1.0'})
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Konstantin Lifschitz events',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = [record for event in events if (record := parse_event(event))]
        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ))


def main():
    KonstantinLifschitzComCrawler().run()


if __name__ == '__main__':
    main()
