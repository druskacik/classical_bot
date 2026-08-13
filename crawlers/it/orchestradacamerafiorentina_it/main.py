import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestradacamerafiorentina.it/'
SOURCE = 'Orchestra da Camera Fiorentina'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# This venue record omits its city in the first-party API. Its name uniquely
# identifies Florence's Luigi Cherubini conservatory.
VENUE_CITY_DEFAULTS = {
    'conservatorio di musica l. cherubini': 'Firenze',
}


def clean_html(value):
    if not value:
        return None
    soup = BeautifulSoup(html.unescape(value), 'html.parser')
    for unwanted in soup.select('script, style'):
        unwanted.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def parse_event(event):
    venue_data = event.get('venue') or {}
    title = clean_html(event.get('title'))
    url = (event.get('url') or '').strip()
    venue = clean_html(venue_data.get('venue'))
    city = clean_html(venue_data.get('city'))
    if not city and venue:
        city = VENUE_CITY_DEFAULTS.get(venue.casefold())

    try:
        start = datetime.fromisoformat(event.get('start_date', ''))
    except (TypeError, ValueError):
        return None

    if not all((title, url, venue, city)):
        return None

    time_from = None if event.get('all_day') else start.strftime('%H:%M')
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': clean_html(event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OrchestradacamerafiorentinaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestradacamerafiorentina_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        page = 1
        events = []

        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'page': page,
                        'per_page': 50,
                        # The API otherwise defaults to upcoming occurrences.
                        'start_date': '1900-01-01 00:00:00',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Orchestra da Camera Fiorentina events',
                    event='crawler_fetch_failed', level='error', url=API_URL,
                    page=page, error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            page_events = payload.get('events')
            if not isinstance(page_events, list):
                raise ValueError('Events API response has no events list')
            events.extend(page_events)

            total_pages = int(payload.get('total_pages') or 1)
            if page >= total_pages:
                break
            page += 1

        records = []
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping Orchestra da Camera Fiorentina event with incomplete details',
                    event='crawler_item_skipped', level='warning',
                    url=(event.get('url') or API_URL), event_id=event.get('id'),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title']),
        )


def main():
    OrchestradacamerafiorentinaItCrawler().run()


if __name__ == '__main__':
    main()
