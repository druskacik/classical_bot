import re
from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.joshvietti.com/'
SHOWS_URL = f'{SOURCE_URL}shows'
SOURCE = 'Josh Vietti'
API_URL = 'https://rest.bandsintown.com/artists/Josh%20Vietti/events'
ARTIST = 'Josh Vietti'

API_PARAMS = {
    'app_id': 'squarespace-josh-vietti-mnnw',
    # The public shows widget uses "upcoming". "all" is the same artist feed
    # with the site's still-published Bandsintown archive included.
    'date': 'all',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': SHOWS_URL,
}

COUNTRY_CODES = {
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'brazil': 'BR',
    'canada': 'CA',
    'china': 'CN',
    'france': 'FR',
    'germany': 'DE',
    'ireland': 'IE',
    'italy': 'IT',
    'japan': 'JP',
    'mexico': 'MX',
    'netherlands': 'NL',
    'new zealand': 'NZ',
    'portugal': 'PT',
    'spain': 'ES',
    'switzerland': 'CH',
    'united kingdom': 'GB',
    'uk': 'GB',
    'united states': 'US',
    'united states of america': 'US',
    'usa': 'US',
    'us': 'US',
}


def clean_text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def clean_description(value):
    if value is None:
        return None
    text = str(value).replace('\r\n', '\n').replace('\r', '\n').replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(clean_text(value).replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def country_code(value):
    country = clean_text(value)
    if re.fullmatch(r'[A-Za-z]{2}', country):
        return country.upper()
    return COUNTRY_CODES.get(country.casefold())


def normalized_venue(value, description):
    venue = clean_text(value)
    artist_at = re.fullmatch(rf'{re.escape(ARTIST)}\s+at\s+(.+)', venue, re.IGNORECASE)
    if artist_at:
        return artist_at.group(1).strip()

    live_at = re.search(r'\bLive at\s+(.+?)(?:\s+[–—-]\s+|$)', venue, re.IGNORECASE)
    if live_at:
        return live_at.group(1).strip()

    location = re.search(
        r'(?:Festival\s+)?Location\s*:\s*\n\s*([^\n]+)',
        str(description or ''),
        re.IGNORECASE,
    )
    if location:
        return clean_text(location.group(1))
    return venue


def parse_event(event):
    venue_data = event.get('venue') or {}
    listed_name = clean_text(venue_data.get('name'))
    venue = normalized_venue(listed_name, event.get('description'))
    city = clean_text(venue_data.get('city'))
    event_date, time_from = parse_datetime(event.get('datetime') or event.get('starts_at'))
    event_country_code = country_code(venue_data.get('country'))
    event_id = clean_text(event.get('id'))

    # Virtual events in the archive have no physical city/country. The project
    # requires a defensible place, so those records are deliberately skipped.
    if not all((event_id, event_date, venue, city, event_country_code)):
        return None

    title = clean_text(event.get('title'))
    if not title:
        title = listed_name if ARTIST.casefold() in listed_name.casefold() else f'{ARTIST} at {venue}'
    return {
        'title': title,
        'date': event_date,
        'url': f'https://www.bandsintown.com/e/{event_id}',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': event_country_code,
        'description': clean_description(event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(API_URL, params=API_PARAMS, headers=HEADERS, timeout=45)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError('Bandsintown artist endpoint did not return an event list')

    records = []
    skipped_count = 0
    for event in payload:
        if not isinstance(event, dict):
            skipped_count += 1
            continue
        record = parse_event(event)
        if record is None:
            skipped_count += 1
            continue
        records.append(record)

    if skipped_count:
        log_message(
            'Skipped events without a complete physical location or valid date',
            event='crawler_records_skipped',
            level='info',
            url=API_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No valid concert candidates found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class JoshViettiComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='joshvietti_com',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    JoshViettiComCrawler().run()


if __name__ == '__main__':
    main()
