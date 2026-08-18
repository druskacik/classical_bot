import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.clevelandorchestra.com/'
SOURCE = 'The Cleveland Orchestra'
INSTANCES_API = urljoin(SOURCE_URL, 'api/event-instances.json')
EVENTS_API = urljoin(SOURCE_URL, 'api/events.json')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

# The API exposes venue names but not postal addresses. These are the venues
# represented by the first-party calendar feed, including Orchestra tour dates.
VENUE_LOCATIONS = {
    'adrienne-arsht-center': ('Miami', 'US'),
    'artis-naples': ('Naples', 'US'),
    'blossom-music-center': ('Cuyahoga Falls', 'US'),
    'brucknerhaus': ('Linz', 'AT'),
    'cain-park': ('Cleveland Heights', 'US'),
    'cankarjev-dom': ('Ljubljana', 'SI'),
    'carnegie-hall': ('New York', 'US'),
    'elbphilharmonie': ('Hamburg', 'DE'),
    'konzerthaus-dortmund-de': ('Dortmund', 'DE'),
    'konzerthaus-vienna-aust': ('Vienna', 'AT'),
    'mandel-concert-hall': ('Cleveland', 'US'),
    'megaron': ('Athens', 'GR'),
    'musikverein': ('Vienna', 'AT'),
    'national-concert-hall-muepa-budapest': ('Budapest', 'HU'),
    'palais-des-beaux-arts-bozar': ('Brussels', 'BE'),
    'philharmonie-de-paris': ('Paris', 'FR'),
    'philharmonie-luxembourg': ('Luxembourg', 'LU'),
    'reduta-hall': ('Bratislava', 'SK'),
    'reinberger-chamber-hall': ('Cleveland', 'US'),
    'severance-music-center': ('Cleveland', 'US'),
    'thessaloniki-concert-hall': ('Thessaloniki', 'GR'),
}


def clean_html(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def event_description(event):
    parts = []
    for value in [event.get('description')]:
        text = clean_html(value)
        if text and text not in parts:
            parts.append(text)

    details = event.get('attributes', {}).get('custom', {}).get('details') or []
    for detail in details:
        text = clean_html(detail.get('description_html'))
        if text and text not in parts:
            parts.append(text)

    repertoire = []
    for work in event.get('works') or []:
        title = clean_html(work.get('title'))
        if title and title not in repertoire:
            repertoire.append(title)
    if repertoire:
        parts.append('Repertoire: ' + '; '.join(repertoire))
    return '\n\n'.join(parts) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    instances_response = session.get(INSTANCES_API, timeout=90)
    instances_response.raise_for_status()
    events_response = session.get(EVENTS_API, timeout=90)
    events_response.raise_for_status()

    instances_payload = instances_response.json()
    events_payload = events_response.json()
    events = {item.get('id'): item for item in events_payload.get('docs', [])}

    records = []
    skipped_venues = set()
    for instance in instances_payload.get('docs', []):
        embedded_event = instance.get('event') or {}
        event = events.get(embedded_event.get('id'), embedded_event)
        venue = instance.get('venue') or event.get('venue') or {}
        venue_name = clean_html(venue.get('title'))
        location = VENUE_LOCATIONS.get(venue.get('slug'))
        title = clean_html(event.get('title'))
        local_start = instance.get('startDateLocalAsUTC') or event.get('startDateLocalAsUTC')
        relative_url = instance.get('url') or event.get('url')

        if not location:
            if venue_name:
                skipped_venues.add(venue_name)
            continue
        if not title or not local_start or not relative_url or len(local_start) < 10:
            continue

        city, country_code = location
        time_from = None
        if not event.get('hideStartTime') and len(local_start) >= 16:
            time_from = local_start[11:16]
        records.append({
            'title': title,
            'date': local_start[:10],
            'url': urljoin(SOURCE_URL, relative_url),
            'time_from': time_from,
            'venue': venue_name,
            'city': city,
            'country_code': country_code,
            'description': event_description(event),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if skipped_venues:
        log_message(
            'Skipped event instances with unmapped venues',
            event='crawler_unmapped_venues',
            level='warning',
            venues=sorted(skipped_venues),
            record_count=len(skipped_venues),
        )
    if not records:
        log_message(
            'No event instances found',
            event='crawler_empty_listing',
            level='warning',
            url=INSTANCES_API,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ClevelandOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='clevelandorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ClevelandOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
