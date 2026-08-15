import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filarmonicabanatul.ro/'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Filarmonica Banatul Timișoara'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'ro-RO,ro;q=0.9,en;q=0.7',
}

# A few first-party venue records omit their city even though the venue name or
# address identifies it unambiguously. Unknown locations are skipped.
VENUE_CITIES = {
    'Biserica Romano-Catolică Vinga': 'Vinga',
    'Casa de Cultură Moldova Nouă': 'Moldova Nouă',
    'Casa de Cultură Recaș': 'Recaș',
    'Conacul Mocioni': 'Foeni',
    'Hotel Padeșul Făget': 'Făget',
    'Sala de Conferințe a Primăriei Bocșa': 'Bocșa',
    'Sat Ofsenița': 'Ofsenița',
    'Școala Gimnazială Jimbolia': 'Jimbolia',
}


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value))
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_pages(session):
    params = {
        'page': 1,
        'per_page': 50,
        # Explicit bounds make the API return its retained archive as well as
        # future events. Without them it defaults to upcoming events only.
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
    }
    while True:
        response = session.get(EVENTS_API, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        yield from payload.get('events') or []

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city')) or VENUE_CITIES.get(venue, '')

    start = clean_text(event.get('start_date'))
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):\d{2}', start)
    if not title or not url or not venue or not city or not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None if event.get('all_day') else f'{match.group(2)}:{match.group(3)}'
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': clean_text(event.get('description')) or None,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    try:
        events = event_pages(session)
        for event in events:
            record = make_record(event)
            if record:
                records.append(record)
    except (requests.RequestException, ValueError, TypeError) as error:
        log_message(
            'Failed to retrieve event feed',
            event='crawler_feed_failed',
            level='error',
            url=EVENTS_API,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FilarmonicaBanatulRoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonicabanatul_ro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FilarmonicaBanatulRoCrawler().run()


if __name__ == '__main__':
    main()
