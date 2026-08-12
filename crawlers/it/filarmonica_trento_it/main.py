import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filarmonica-trento.it/'
EVENTS_API = f'{SOURCE_URL}wp-json/stec/v5/events'
SOURCE = 'Fondazione Filarmonica Trento'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = str(value)
    if re.search(r'<[a-z!/][^>]*>', text, re.I):
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def clean_city(value):
    city = clean_text(value)
    city = re.sub(r'^\d{5}\s+', '', city)
    city = re.sub(r'\s*\((?:TN|BZ)\)\s*$', '', city, flags=re.I)
    if ',' in city:
        city = city.split(',', 1)[0].strip()
    return city


def parse_location(location):
    if not isinstance(location, dict):
        return None

    title = clean_text(location.get('title'))
    address = clean_text(location.get('address'))
    city = clean_city(location.get('city'))

    if address.casefold().startswith('via giuseppe verdi, 30'):
        return 'Sala Filarmonica', city or 'Trento'
    if address.casefold().startswith('corso del lavoro e della scienza 3'):
        return 'MUSE - Museo delle Scienze', city or 'Trento'

    # The API sometimes uses a street address as the location title. Such a
    # value is not a venue, so omit the occurrence unless a named place exists.
    looks_like_address = bool(
        re.match(r'^(?:via|viale|piazza|corso|strada|vicolo|localit[aà])\b', title, re.I)
        or re.search(r'\b\d{5}\b', title)
    )
    venue = '' if looks_like_address else title
    if not city and venue:
        # A few touring locations omit the city field but name the municipality.
        for candidate in ('Caldes', 'Stenico', "Ville d'Anaunia"):
            if candidate.casefold() in venue.casefold():
                city = candidate
                break
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue, city


def parse_event(event):
    start = parse_start((event.get('meta') or {}).get('start_date'))
    location = parse_location(event.get('location'))
    title = clean_text(event.get('title'))
    url = clean_text(event.get('permalink'))
    if not start or not location or not title or not url:
        return None

    event_date, time_from = start
    venue, city = location
    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
    }


class FilarmonicaTrentoItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonica_trento_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            params = {
                'context': 'event',
                'per_page': 100,
                'page': page,
                'order': 'asc',
            }
            try:
                response = session.get(EVENTS_API, params=params, timeout=45)
                response.raise_for_status()
                events = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Filarmonica Trento events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=EVENTS_API,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            if not isinstance(events, list):
                raise ValueError('Filarmonica Trento events API returned a non-list response')

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FilarmonicaTrentoItCrawler().run()


if __name__ == '__main__':
    main()
