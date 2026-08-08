import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bijloke.be/'
AGENDA_URL = urljoin(SOURCE_URL, 'nl/programma')
SOURCE = 'Muziekcentrum De Bijloke'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-BE,nl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def add_event_urls(soup, urls):
    for card in soup.select('.eventCard'):
        link = card.select_one('a[href]')
        if not link:
            continue
        url = urljoin(SOURCE_URL, link.get('href'))
        path = urlparse(url).path
        if path.startswith('/nl/programma/') and path.count('/') == 3:
            urls.add(url)


def listing_urls(session):
    # The public date picker explicitly supports history. A wide fixed range
    # discovers the complete retained archive as well as announced seasons.
    params = {'start': '2000-01-01', 'end': '2100-12-31', 'page': 1}
    first = get_soup(session, AGENDA_URL, params=params)
    pages = [
        int(option.get('value'))
        for option in first.select('#pagination-select option[value]')
        if option.get('value', '').isdigit()
    ]
    last_page = max(pages, default=1)
    urls = set()
    add_event_urls(first, urls)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                get_soup,
                session,
                AGENDA_URL,
                {'start': params['start'], 'end': params['end'], 'page': page},
            ): page
            for page in range(2, last_page + 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                add_event_urls(future.result(), urls)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape agenda page',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{AGENDA_URL}?page={page}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(urls)


def event_objects(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        events.extend(
            item for item in items
            if isinstance(item, dict) and item.get('@type') == 'Event'
        )
    return events


def parse_start(value):
    if not value:
        return None
    try:
        starts_at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        event_date = date(starts_at.year, starts_at.month, starts_at.day).isoformat()
    except (TypeError, ValueError):
        return None
    time_from = starts_at.strftime('%H:%M') if 'T' in str(value) else None
    return event_date, time_from


def resolve_location(event, soup):
    location = event.get('location') or {}
    if not isinstance(location, dict):
        location = {}
    address = location.get('address') or {}
    if not isinstance(address, dict):
        address = {}
    city = clean_text(address.get('addressLocality'))
    location_name = clean_text(location.get('name'))
    room = clean_text(soup.select_one('.showHeader .venue'))
    venue = room or location_name

    # De Bijloke sometimes publishes visiting/off-site events. Structured
    # address data remains authoritative; the Gent default is only used when
    # the event explicitly names the home complex.
    if not city and 'bijloke' in location_name.lower():
        city = 'Gent'
    if not venue or not city:
        return None
    return venue, city


def detail_description(soup, event):
    parts = []
    for block in soup.select('main .richtext'):
        text = clean_text(block)
        if text and text not in parts:
            parts.append(text)
    description = clean_text('\n\n'.join(parts))
    return description or clean_text(event.get('description')) or None


def make_records(url, soup):
    records = []
    page_title = clean_text(soup.select_one('main h1'))
    for event in event_objects(soup):
        title = page_title or clean_text(event.get('name'))
        start = parse_start(event.get('startDate'))
        location = resolve_location(event, soup)
        if not title or not start or not location:
            continue
        event_date, time_from = start
        venue, city = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'BE',
            'description': detail_description(soup, event),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(make_records(url, future.result()))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
    )


class BijlokeBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bijloke_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BijlokeBeCrawler().run()


if __name__ == '__main__':
    main()
