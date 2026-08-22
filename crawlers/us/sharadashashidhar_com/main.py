import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sharadashashidhar.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
EVENTS_API_URL = f'{EVENTS_URL}?format=json'
SONGKICK_API_URL = 'https://widget-app.songkick.com/api/calendar/10268200'
SOURCE = 'Sharada Shashidhar'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'united states': 'US',
    'us': 'US',
    'usa': 'US',
    'canada': 'CA',
    'ca': 'CA',
    'mexico': 'MX',
    'united kingdom': 'GB',
    'uk': 'GB',
    'germany': 'DE',
    'france': 'FR',
    'netherlands': 'NL',
    'spain': 'ES',
    'italy': 'IT',
}

US_TIMEZONES = {
    'CA': 'America/Los_Angeles',
    'CO': 'America/Denver',
    'OR': 'America/Los_Angeles',
    'WA': 'America/Los_Angeles',
    'AZ': 'America/Phoenix',
    'NM': 'America/Denver',
    'TX': 'America/Chicago',
    'IL': 'America/Chicago',
    'NY': 'America/New_York',
    'MA': 'America/New_York',
    'PA': 'America/New_York',
    'DC': 'America/New_York',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def country_code(value):
    cleaned = clean_text(value)
    if re.fullmatch(r'[A-Za-z]{2}', cleaned):
        return cleaned.upper()
    return COUNTRY_CODES.get(cleaned.lower())


def collection_items(session):
    url = EVENTS_API_URL
    seen_ids = set()
    items = []
    while url:
        payload = get_json(session, url)
        for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
            item_id = item.get('id')
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                items.append(item)

        next_url = (payload.get('pagination') or {}).get('nextPageUrl')
        if next_url:
            separator = '&' if '?' in next_url else '?'
            url = urljoin(SOURCE_URL, f'{next_url}{separator}format=json')
        else:
            url = None
    return items


def collection_location(item):
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    address_line = clean_text(location.get('addressLine2'))
    city = address_line.split(',', 1)[0].strip()
    code = country_code(location.get('addressCountry'))
    # One archived Los Angeles entry omits the country even though its state,
    # city, and venue are explicit.
    if not code and re.search(r'(?:^|,)\s*CA(?:,|$)', address_line):
        code = 'US'
    if not venue or venue.lower() == 'unknown venue' or not city or not code:
        return None
    return venue, city, code, address_line


def collection_record(item):
    title = clean_text(item.get('title'))
    location = collection_location(item)
    full_url = item.get('fullUrl')
    start_ms = item.get('startDate')
    if not title or not location or not full_url or not isinstance(start_ms, (int, float)):
        return None

    venue, city, code, address_line = location
    region_match = re.search(r',\s*([A-Z]{2})(?:,|\s|$)', address_line)
    timezone_name = US_TIMEZONES.get(region_match.group(1) if region_match else '')
    timezone_name = timezone_name or 'America/Los_Angeles'
    try:
        start = datetime.fromtimestamp(start_ms / 1000, tz=ZoneInfo(timezone_name))
    except (OSError, OverflowError, ValueError):
        return None

    description_parts = [clean_text(item.get('excerpt')), clean_text(item.get('body'))]
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def songkick_records(session):
    payload = get_json(session, SONGKICK_API_URL)
    performances = (
        ((payload.get('resultsPage') or {}).get('results') or {}).get('performance') or []
    )
    records = []
    for wrapper in performances:
        event = wrapper.get('event') or {}
        start = event.get('start') or {}
        venue_data = event.get('venue') or {}
        metro = venue_data.get('metroArea') or {}
        venue = clean_text(venue_data.get('displayName'))
        city = clean_text(metro.get('displayName'))
        city = re.sub(r'\s*\([^)]*\)\s*$', '', city).strip()
        code = country_code((metro.get('country') or {}).get('displayName'))
        title = clean_text(event.get('displayName'))
        event_date = clean_text(start.get('date'))
        event_url = clean_text(event.get('uri'))
        if (
            not title or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', event_date)
            or not event_url or not venue or venue.lower() == 'unknown venue'
            or not city or not code
        ):
            continue
        try:
            datetime.strptime(event_date, '%Y-%m-%d')
        except ValueError:
            continue

        artists = [
            clean_text(performance.get('displayName'))
            for performance in event.get('performance') or []
        ]
        description = f"Performers: {', '.join(artist for artist in artists if artist)}"
        records.append({
            'title': title,
            'date': event_date,
            'url': event_url,
            'time_from': clean_text(start.get('time'))[:5] or None,
            'venue': venue,
            'city': city,
            'country_code': code,
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class SharadaShashidharComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sharadashashidhar_com',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        try:
            records.extend(
                record for item in collection_items(session)
                if (record := collection_record(item))
            )
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Squarespace event archive',
                event='crawler_feed_failed',
                level='warning',
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        try:
            records.extend(songkick_records(session))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Songkick calendar',
                event='crawler_feed_failed',
                level='warning',
                url=SONGKICK_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        if not records:
            raise RuntimeError('No valid events returned by either event feed')
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    SharadaShashidharComCrawler().run()


if __name__ == '__main__':
    main()
