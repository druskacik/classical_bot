import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.drkoncerthuset.dk/'
EVENTS_API = urljoin(SOURCE_URL, 'umbraco/api/events/fetch')
SOURCE = 'DR Koncerthuset'
CITY = 'København'

VENUE_CITIES = {
    'Garnisonskirken': CITY,
    'Gudhjem Kirke': 'Gudhjem',
    'Holmens Kirke': CITY,
    'Koncertsalen': CITY,
    'Koncertsalen Alsion, Sønderborg': 'Sønderborg',
    'Marmorkirken (Frederikskirken)': CITY,
    'ODEON, Odense': 'Odense',
    'Roskilde Domkirke': 'Roskilde',
    'Sankt Nicolai Kirke i Rønne': 'Rønne',
    'Sct. Bendts Kirke, Ringsted': 'Ringsted',
    'Sorø Klosterkirke': 'Sorø',
    'Studie 1': CITY,
    'Studie 2': CITY,
    'Studie 3': CITY,
    'Trinitatis Kirke, København': CITY,
    'Viborg Domkirke': 'Viborg',
    'Vor Frue Kirke, Københavns Domkirke': CITY,
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    # This is the same public endpoint used by the site's calendar cards. A
    # large page size returns every concrete event; totalItems also counts
    # non-event/dynamic entries which are intentionally absent from results.
    response = session.get(EVENTS_API, params={'pageSize': 1000}, timeout=60)
    response.raise_for_status()
    return response.json().get('results') or []


def detail_description(session, url, fallback):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    parts = []
    for selector in ('.event-page__main-content', '.program'):
        element = soup.select_one(selector)
        value = clean_text(element)
        if value and value not in parts:
            parts.append(value)
    return clean_text('\n\n'.join(parts)) or clean_text(fallback) or None


def make_record(event, description=None):
    title = clean_text(event.get('title'))
    subtitle = clean_text(event.get('subTitle'))
    if subtitle and subtitle.lower() not in title.lower():
        title = f'{title} – {subtitle}'

    event_path = event.get('url')
    venue = clean_text(event.get('venue'))
    city = VENUE_CITIES.get(venue)
    start = event.get('orderDate') or ''
    try:
        parsed = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if not title or not event_path or not venue or not city:
        return None

    fallback = (event.get('info') or {}).get('description')
    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': urljoin(SOURCE_URL, event_path),
        'time_from': parsed.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'DK',
        'description': description or clean_text(fallback) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {}
        for event in events:
            record = make_record(event)
            if record:
                fallback = (event.get('info') or {}).get('description')
                future = executor.submit(
                    detail_description, session, record['url'], fallback
                )
                futures[future] = (event, record)

        for future in as_completed(futures):
            event, record = futures[future]
            try:
                record['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
            records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class DrkoncerthusetDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='drkoncerthuset_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
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
        return get_concerts()


def main():
    DrkoncerthusetDkCrawler().run()


if __name__ == '__main__':
    main()
