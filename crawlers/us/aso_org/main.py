import json
import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.aso.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-tickets')
SOURCE = 'Atlanta Symphony Orchestra'

VENUE_CITIES = {
    'Ameris Bank Amphitheatre': 'Alpharetta',
    'Spivey Hall at Clayton State University': 'Morrow',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def month_sequence(first_year, first_month, last_year, last_month):
    year, month = first_year, first_month
    while (year, month) <= (last_year, last_month):
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


def parse_json_ld(soup, page_url):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict) or item.get('@type') != 'Event':
                continue
            if item.get('url', '').rstrip('/') != page_url.rstrip('/'):
                continue
            location = item.get('location') or {}
            address = location.get('address') or {}
            return {
                'venue': clean_text(location.get('name')),
                'city': clean_text(address.get('addressLocality')),
                'description': clean_text(item.get('description')) or None,
            }
    return {}


def fetch_event_details(session, url, listing_venue):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    if response.url.rstrip('/') != url.rstrip('/'):
        return {}
    details = parse_json_ld(BeautifulSoup(response.text, 'html.parser'), url)
    details['venue'] = listing_venue or details.get('venue', '')
    if details.get('venue') in VENUE_CITIES:
        details['city'] = VENUE_CITIES[details['venue']]
    elif details.get('venue') == 'Atlanta Symphony Hall':
        details['city'] = 'Atlanta'
    return details


def fetch_listing(session):
    events = {}
    offset = 0
    while True:
        response = session.get(
            urljoin(SOURCE_URL, f'events/events_ajax/{offset}'),
            params={
                'category': 0,
                'venue': 0,
                'team': 0,
                'exclude': '',
                'per_page': 12,
                'came_from_page': 'event-list-page',
            },
            timeout=45,
        )
        response.raise_for_status()
        try:
            html = response.json()
        except requests.exceptions.JSONDecodeError:
            html = response.text
        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.eventItem')
        if not cards:
            break
        for card in cards:
            link = card.select_one('h3 a[href]')
            if not link:
                continue
            url = urljoin(SOURCE_URL, link['href'])
            date_text = clean_text(card.select_one('.date'))
            years = [int(value) for value in re.findall(r'\b(20\d{2})\b', date_text)]
            events[url] = {
                'title': clean_text(link),
                'venue': clean_text(card.select_one('.location')),
                'last_year': max(years) if years else date.today().year,
            }
        offset += len(cards)
    return events


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = fetch_listing(session)
    if not events:
        log_message(
            'No events found in ASO listing',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
        return []

    today = date.today()
    last_year = max(item['last_year'] for item in events.values())
    # Event ranges on this site do not extend beyond the final season year.
    last_month = 12 if last_year > today.year else 12
    occurrences = []
    for year, month in month_sequence(today.year, today.month, last_year, last_month):
        response = session.get(
            urljoin(SOURCE_URL, f'events/calendar/{year}/{month}'),
            params={'v': 2},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        for html in payload.values():
            if not isinstance(html, str):
                continue
            soup = BeautifulSoup(html, 'html.parser')
            for card in soup.select('.event_item_wrapper'):
                link = card.select_one('h3 a[href]')
                date_node = card.select_one('.date .dt')
                if not link or not date_node:
                    continue
                url = urljoin(SOURCE_URL, link['href'])
                if url not in events:
                    continue
                try:
                    event_date = datetime.strptime(clean_text(date_node), '%b %d, %Y').date().isoformat()
                except ValueError:
                    continue
                time_text = clean_text(card.select_one('.date .time')).lstrip('- ').strip()
                try:
                    time_from = datetime.strptime(time_text, '%I:%M %p').strftime('%H:%M')
                except ValueError:
                    time_from = None
                occurrences.append((url, event_date, time_from))

    details_cache = {}
    records = []
    for url, event_date, time_from in occurrences:
        if url not in details_cache:
            try:
                details_cache[url] = fetch_event_details(session, url, events[url]['venue'])
            except requests.RequestException as error:
                log_message(
                    'ASO event detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                details_cache[url] = {}
        details = details_cache[url]
        venue = details.get('venue', '')
        city = details.get('city', '')
        if not venue or not city:
            continue
        records.append({
            'title': events[url]['title'],
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': details.get('description'),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class AsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aso_org',
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
    AsoOrgCrawler().run()


if __name__ == '__main__':
    main()
