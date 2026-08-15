import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://caramoor.org/'
LISTING_URL = f'{SOURCE_URL}events/concerts'
SOURCE = 'Caramoor Center for Music and the Arts'
CITY = 'Katonah'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# These are the first-party venue tags exposed by the events page. More
# specific spaces take precedence when an event also carries Rosen House or
# Caramoor Grounds as a parent location.
VENUES = {
    'education-center': 'Diane Moss Education Center',
    'friends-field': 'Friends Field',
    'music-room': 'Rosen House Music Room',
    'pavilion-tent': 'Pavilion Tent',
    'spanish-courtyard': 'Spanish Courtyard',
    'sunken-garden': 'Sunken Garden',
    'venetian-theater': 'Venetian Theater',
    'rosen-house': 'Rosen House',
    'caramoor-grounds': 'Caramoor Center for Music and the Arts',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def extract_catalog(html):
    """Decode the React Query catalog embedded in the Next.js response."""
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script'):
        source = script.get_text()
        if 'eventList' not in source or 'self.__next_f.push(' not in source:
            continue
        try:
            payload = json.loads(source[len('self.__next_f.push('):-1])[1]
            query_at = payload.index('"queries"')
            state_at = payload.rfind('{"state":', 0, query_at)
            root, _ = json.JSONDecoder().raw_decode(payload[state_at:])
            return root['state']['queries'][0]['state']['data']
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            continue
    raise ValueError('Could not locate the embedded Caramoor event catalog')


def select_venue(event):
    slugs = {tag.get('slug') for tag in event.get('tags', [])}
    for slug, venue in VENUES.items():
        if slug in slugs:
            return venue
    return ''


def detail_description(session, url, fallback):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        parts = []
        # Overview and Program are the useful event-level editorial sections.
        # Artist biographies and venue marketing that follow are intentionally
        # excluded, while the programme remains available for later analysis.
        for heading in soup.find_all('h2'):
            label = clean_text(heading.get_text(' ', strip=True)).lower()
            if label not in {'overview', 'program', 'programme'}:
                continue
            section = heading.parent
            text = clean_text(section.get_text('\n', strip=True))
            if text and text not in parts:
                parts.append(text)
        return '\n\n'.join(parts) or clean_text(fallback) or None
    except requests.RequestException as error:
        log_message(
            'Event detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return clean_text(fallback) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=60)
    response.raise_for_status()
    catalog = extract_catalog(response.text)

    events = {
        event['spektrixEventId']: event
        for event in catalog.get('events', [])
        if event.get('spektrixEventId')
        and any(tag.get('slug') == 'concerts' for tag in event.get('tags', []))
    }
    records = []
    for instance in catalog.get('instances', []):
        ticket_event = instance.get('event') or {}
        event = events.get(ticket_event.get('id'))
        if not event:
            continue
        title = clean_text(event.get('name') or ticket_event.get('name'))
        venue = select_venue(event)
        start = instance.get('start')
        url = f"{SOURCE_URL}event/{event.get('slug', '')}"
        try:
            parsed_start = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            continue
        if not title or not venue or not event.get('slug'):
            continue
        records.append({
            'title': title,
            'date': parsed_start.date().isoformat(),
            'url': url,
            'time_from': parsed_start.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': clean_text(ticket_event.get('description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    descriptions = {}
    urls = {record['url']: record['description'] for record in records}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, url, fallback): url
            for url, fallback in urls.items()
        }
        for future in as_completed(futures):
            descriptions[futures[future]] = future.result()
    for record in records:
        record['description'] = descriptions.get(record['url'], record['description'])

    if not records:
        log_message(
            'No concert candidates found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class CaramoorOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='caramoor_org',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CaramoorOrgCrawler().run()


if __name__ == '__main__':
    main()
