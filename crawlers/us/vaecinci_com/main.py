import json
import re
from html import unescape
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://vaecinci.com/'
SOURCE = 'VAE Cincinnati'
EVENTS_URL = f'{SOURCE_URL}events/list/'
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
    raw = unescape(str(value))
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\\n', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_json_ld(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        events.extend(
            value for value in values
            if isinstance(value, dict) and value.get('@type') == 'Event'
        )
    return events


def parse_start(value):
    try:
        start = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return '', None
    return start.date().isoformat(), start.strftime('%H:%M')


def parse_event(event, description=None):
    location = event.get('location') or {}
    address = location.get('address') or {}
    title = clean_text(event.get('name'))
    url = clean_text(event.get('url'))
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    date, time_from = parse_start(event.get('startDate'))

    # Every event published by this Cincinnati-based ensemble is local in the
    # available calendar. Some otherwise complete venue records omit locality.
    if not city and (
        re.search(r'\bCincinnati\b', description or '', re.I)
        or clean_text(address.get('streetAddress')) == '2161 GRANDIN RD.'
    ):
        city = 'Cincinnati'

    if not all((title, date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class VaecinciComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='vaecinci_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def get_soup(self, session, url):
        response = session.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')

    def listing_events(self, session):
        events_by_url = {}
        visited = set()
        feeds = (
            (EVENTS_URL, 'Next Events'),
            (f'{EVENTS_URL}?eventDisplay=past', 'Previous Events'),
        )
        for first_url, direction in feeds:
            page_url = first_url
            while page_url and page_url not in visited:
                visited.add(page_url)
                soup = self.get_soup(session, page_url)
                for event in event_json_ld(soup):
                    url = clean_text(event.get('url'))
                    if url:
                        events_by_url[url] = event

                link = soup.select_one(f'a[aria-label="{direction}"]')
                next_url = urljoin(page_url, link.get('href')) if link else None
                page_url = next_url if next_url not in visited else None
        return list(events_by_url.values())

    def scrape(self):
        session = requests.Session()
        records = []
        for event in self.listing_events(session):
            url = clean_text(event.get('url'))
            try:
                detail_soup = self.get_soup(session, url)
                body = detail_soup.select_one('.tribe-events-single-event-description')
                description = clean_text(body) or None
                detail_events = event_json_ld(detail_soup)
                detail_event = detail_events[0] if detail_events else event
                record = parse_event(detail_event, description)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch VAE Cincinnati event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                record = parse_event(event)

            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete VAE Cincinnati event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, or city is missing',
                )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    VaecinciComCrawler().run()


if __name__ == '__main__':
    main()
