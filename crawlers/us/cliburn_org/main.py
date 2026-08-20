import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cliburn.org/'
SOURCE = 'The Cliburn'
API_URL = 'https://cliburn.org/api/idfive_calendar/events'
ARCHIVE_START = '2000-01-01'
PAGE_SIZE = 50

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        raw = html.unescape(str(value))
        text = (
            BeautifulSoup(raw, 'html.parser').get_text(separator, strip=True)
            if '<' in raw
            else raw
        )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%I:%M %p').strftime('%H:%M')
    except (AttributeError, ValueError):
        return None


def event_description(event):
    summary = clean_text(event.get('summary'), separator='\n')
    description = clean_text(event.get('description'), separator='\n')
    if summary and description and summary not in description:
        return f'{summary}\n\n{description}'
    return description or summary or None


def location_from_page(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    address = soup.select_one('.event-detail-hero__column .address')
    if address is None:
        return None

    venue_parts = [
        clean_text(address.select_one(f'.address-line{number}'))
        for number in (1, 2)
    ]
    if len(venue_parts) > 1 and re.search(r'\d', venue_parts[1]):
        venue_parts[1] = ''
    venue = ', '.join(part for part in venue_parts if part)
    if re.match(r'^\d', venue):
        page_text = clean_text(soup)
        venue = 'Omni Theater' if 'Omni Theater' in page_text else ''
    city = clean_text(address.select_one('.locality'))
    country = clean_text(address.select_one('.country')).casefold()
    if not venue or not city:
        return None

    country_code = {
        'united states': 'US',
        'usa': 'US',
        'canada': 'CA',
        'mexico': 'MX',
    }.get(country)
    if not country_code:
        return None
    return venue, city, country_code


def api_location(event):
    venues = event.get('category_2') or []
    venue = clean_text(venues[0].get('name')) if venues else ''
    if not venue:
        return None
    return venue, 'Fort Worth', 'US'


def fetch_events(session):
    events = []
    offset = 0
    total = None
    while total is None or offset < total:
        params = {
            'range_start': offset,
            'range_total': PAGE_SIZE,
            'start_date': ARCHIVE_START,
            'taxonomy': 'idfive_calendar_taxonomy_1,idfive_calendar_taxonomy_2',
        }
        response = session.get(API_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        page = payload.get('data')
        if not isinstance(page, list):
            raise ValueError('Cliburn calendar API returned an unexpected response')
        try:
            total = int(payload['query']['total'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Cliburn calendar API omitted its result total') from error
        events.extend(page)
        if not page:
            break
        offset += len(page)
    return events


def fetch_page_location(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return location_from_page(response.text)


def resolve_locations(events):
    locations = {}
    unresolved = []
    for event in events:
        url = clean_text(event.get('url'))
        location = api_location(event)
        if url and location:
            locations[url] = location
        elif url:
            unresolved.append(url)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_page_location, url): url for url in set(unresolved)}
        for future in as_completed(futures):
            url = futures[future]
            try:
                location = future.result()
                if location:
                    locations[url] = location
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Cliburn event location',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return locations


def record_from_event(event, locations):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = (event.get('date') or {}).get('start') or {}
    event_date = parse_date(start.get('date'))
    location = locations.get(url)
    if not title or not url or not event_date or not location:
        return None

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(start.get('time')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': event_description(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CliburnOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cliburn_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_events(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Cliburn calendar',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        locations = resolve_locations(events)
        records = [record_from_event(event, locations) for event in events]
        records = [record for record in records if record]
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    CliburnOrgCrawler().run()


if __name__ == '__main__':
    main()
