import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lfo.org.uk/'
SOURCE = 'Longborough Festival Opera'
EVENTS_URL = urljoin(SOURCE_URL, 'whats-on')
ARCHIVE_URL = urljoin(SOURCE_URL, 'archive')
API_URL = 'https://system.spektrix.com/longboroughfestivalopera/api/v3/'
VENUE = 'Longborough Festival Opera'
CITY = 'Longborough'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    else:
        value = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def normalized_title(value):
    return re.sub(r'[^a-z0-9]+', '', clean_text(value).lower())


def production_links(soup):
    links = {}
    for anchor in soup.select('a.news-block__link[href]'):
        url = urljoin(SOURCE_URL, anchor.get('href', '')).split('#', 1)[0]
        path = urlparse(url).path
        if not re.match(r'^/(?:opera|archive/\d{4})/[^/]+/?$', path):
            continue
        heading = anchor.select_one('h1, h2, h3, h4, .news-block__title')
        title = clean_text(heading)
        if not title:
            title = clean_text(anchor)
            title = re.split(r'\b\d{1,2}\s+[A-Z][a-z]+\b', title, maxsplit=1)[0]
        if title:
            links[normalized_title(title)] = url
    return links


def page_description(soup):
    tabs = soup.select_one('[data-sc-tabs]')
    if tabs:
        tabs = BeautifulSoup(str(tabs), 'html.parser')
        for node in tabs.select('nav, script, style, .event-gallery'):
            node.decompose()
        text = clean_text(tabs)
        if text:
            return text
    return None


class LfoOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lfo_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def fetch(self, session, url, *, json_response=False):
        last_error = None
        for attempt in range(3):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                return response.json() if json_response else BeautifulSoup(
                    response.text, 'html.parser'
                )
            except (requests.RequestException, ValueError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(attempt + 1)
        raise last_error

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        events = self.fetch(session, urljoin(API_URL, 'events'), json_response=True)
        instances = self.fetch(session, urljoin(API_URL, 'instances'), json_response=True)

        # This is a first-party Spektrix event attribute. Adjacent values are
        # "Other" and "Dining" and contain talks, transport and hospitality.
        performances = {
            event['id']: event
            for event in events
            if event.get('attribute_EventType') == 'Performance'
        }

        links = {}
        for index_url in (EVENTS_URL, ARCHIVE_URL):
            try:
                links.update(production_links(self.fetch(session, index_url)))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch LFO production index',
                    event='crawler_item_failed', level='warning', url=index_url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        descriptions = {}
        records = []
        for instance in instances:
            event_id = (instance.get('event') or {}).get('id')
            event = performances.get(event_id)
            if not event:
                continue
            start = instance.get('start')
            match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):\d{2}', start or '')
            if not match:
                continue
            title = clean_text(event.get('name'))
            page_url = links.get(normalized_title(title), EVENTS_URL)
            if page_url not in descriptions:
                try:
                    descriptions[page_url] = page_description(self.fetch(session, page_url))
                except requests.RequestException as error:
                    descriptions[page_url] = clean_text(event.get('htmlDescription')) or None
                    log_message(
                        'Failed to fetch LFO production page',
                        event='crawler_item_failed', level='warning', url=page_url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
            records.append({
                'title': title,
                'date': match.group(1),
                'url': page_url,
                'time_from': match.group(2),
                'venue': VENUE,
                'city': CITY,
                'country_code': 'GB',
                'description': descriptions[page_url],
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


def main():
    LfoOrgUkCrawler().run()


if __name__ == '__main__':
    main()
