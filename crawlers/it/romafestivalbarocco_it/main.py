import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.romafestivalbarocco.it/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Roma Festival Barocco'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = html.unescape(str(value))
        if re.search(r'<[a-z!/][^>]*>', text, re.I):
            text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def normalize_venue(value):
    venue = clean_text(value)
    venue = re.sub(r'^Roma\s*,\s*', '', venue, flags=re.I)
    venue = re.sub(r'\s+Roma$', '', venue, flags=re.I)
    return venue.strip(' ,-')


def description_venue(description):
    """Recover a venue only when the programme states a recognisable place."""
    for line in description.splitlines()[:8]:
        candidate = re.sub(r'^Roma\s*,\s*', '', line.strip(), flags=re.I)
        if re.search(
            r'\b(?:chiesa|basilica|monastero|refettorio|oratorio|villa|palazzo|sala)\b',
            candidate,
            re.I,
        ):
            candidate = re.sub(r'^\w+\s+\d{1,2}\s+\w+\s+\d{4}(?:\s*,?\s*ore\s*[\d.,:]+)?\s*', '', candidate, flags=re.I)
            venue = normalize_venue(candidate)
            if venue:
                return venue
    return None


def parse_event(event):
    parsed_start = parse_datetime(event.get('start_date'))
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    description = clean_text(event.get('description'))
    venue_data = event.get('venue') or {}
    venue_raw = clean_text(venue_data.get('venue'))
    venue = normalize_venue(venue_raw) or description_venue(description)

    if not parsed_start or not title or not url or not venue:
        return None

    city_text = clean_text(venue_data.get('city'))
    combined_location = f'{venue_raw}\n{description[:300]}'
    if re.search(r'\bFarfa\b|\bFerfa\b', combined_location, re.I):
        city = 'Farfa'
        if re.search(r'\b(?:Farfa|Ferfa)\b.*\bAbbazia', venue, re.I):
            venue = 'Abbazia Benedettina di Farfa'
    elif city_text and not re.search(r'\b(?:via|piazza|viale)\b', city_text, re.I):
        city = city_text
    else:
        # The festival and its calendars are Rome-based; touring locations are
        # explicitly named in the event venue/programme (as with Farfa above).
        city = 'Roma'

    event_date, time_from = parsed_start
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class RomaFestivalBaroccoItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='romafestivalbarocco_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'start_date': '2000-01-01',
            'end_date': '2100-12-31',
            'per_page': 50,
            'page': 1,
        }
        records = []

        while True:
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Roma Festival Barocco events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=params['page'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events')
            if not isinstance(events, list):
                raise ValueError('Roma Festival Barocco API returned no event list')

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Roma Festival Barocco event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(event.get('url')) or API_URL,
                        event_id=event.get('id'),
                    )

            total_pages = payload.get('total_pages', 1)
            try:
                total_pages = int(total_pages)
            except (TypeError, ValueError):
                total_pages = 1
            if params['page'] >= total_pages:
                break
            params['page'] += 1

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['venue'], row['title']),
        )


def main():
    RomaFestivalBaroccoItCrawler().run()


if __name__ == '__main__':
    main()
