import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brandenburgertheater.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'spielplan.html')
EVENTS_API = urljoin(SOURCE_URL, 'kokosConnect/service.php')
SOURCE = 'Brandenburger Theater'
HOME_CITY = 'Brandenburg an der Havel'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
    )))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def city_for_venue(venue):
    """The theatre qualifies tour venues with their city; local halls are not qualified."""
    known_tour_markers = {
        'Nikolaisaal Potsdam': 'Potsdam',
        'Konzerthaus Berlin': 'Berlin',
        'Philharmonie Berlin': 'Berlin',
    }
    for marker, city in known_tour_markers.items():
        if marker.casefold() in venue.casefold():
            return city

    # Preserve explicitly named German cities in future touring entries.
    match = re.search(r'\b(?:in|zu)\s+([A-ZÄÖÜ][\wÄÖÜäöüß.-]+(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß.-]+)*)$', venue)
    if match and 'Brandenburg an der Havel' not in match.group(1):
        return match.group(1)
    return HOME_CITY


def listing_events(session):
    # This is the same structured request made by the public calendar. A zero
    # limit and empty timespan ask it for every published occurrence.
    response = session.post(EVENTS_API, data={
        'action': 'getEvents',
        'genre': '0',
        'month': '0',
        'location': 'all',
        'maxEventAmount': '0',
        'maxTimespan': '',
    }, timeout=45)
    response.raise_for_status()
    payload = response.json()
    return [
        event
        for key, events in payload.items()
        if key != 'moreButton' and isinstance(events, list)
        for event in events
    ]


def parse_listing(events):
    records = []
    for event in events:
        title = clean_text(event.get('pressname'))
        venue = clean_text(event.get('location'))
        event_id = clean_text(event.get('eventId'))
        start = event.get('time')
        if not title or not venue or not event_id or not start:
            continue
        try:
            moment = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            continue
        records.append({
            'title': title,
            'date': moment.date().isoformat(),
            'url': f'{SOURCE_URL}eventdetails?event={event_id}',
            'time_from': moment.strftime('%H:%M'),
            'venue': venue,
            'city': city_for_venue(venue),
            'country_code': 'DE',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def enrich_detail(session, record):
    soup = get_soup(session, record['url'])
    title = clean_text(soup.select_one('.event-title'))
    if title:
        record['title'] = title

    parts = []
    for selector in ('.event-subtitle', '.event-description'):
        value = clean_text(soup.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    record['description'] = '\n\n'.join(parts) or None

    # The detail page is authoritative when it supplies the occurrence venue.
    info = soup.select_one('.termin-infos')
    if info:
        spans = [clean_text(node) for node in info.find_all('span', recursive=False)]
        venue = next((value for value in spans if value and not re.search(r'\d{2}:\d{2}', value)), '')
        if venue:
            record['venue'] = venue
            record['city'] = city_for_venue(venue)
    return record


def get_concerts():
    session = make_session()
    records = parse_listing(listing_events(session))
    enriched = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(enrich_detail, session, record): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Brandenburger Theater event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                enriched.append(record)

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in enriched
    }
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url']
    ))


class BrandenburgertheaterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brandenburgertheater_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BrandenburgertheaterDeCrawler().run()


if __name__ == '__main__':
    main()
