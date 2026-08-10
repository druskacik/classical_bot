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


SOURCE_URL = 'https://www.theater-essen.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'programm/kalender/')
SOURCE = 'Theater und Philharmonie Essen'
CITY = 'Essen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=12,
        pool_maxsize=12,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    if not value:
        return None, None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None, None
    return moment.date().isoformat(), moment.strftime('%H:%M')


def clean_venue(value):
    venue = clean_text(value)
    venue = re.sub(r'^Treffpunkt:\s*(?:Haupteingang\s+)?', '', venue, flags=re.I)
    # Some external venues append a street address after their actual name.
    venue = re.sub(r',\s*[^,]*\d[^,]*$', '', venue).strip()
    return venue


def calendar_urls(session):
    soup = get_soup(session, SCHEDULE_URL)
    urls = {
        urljoin(SOURCE_URL, link.get('href'))
        for link in soup.select('a.calendar__headermonth[href]')
    }
    # The unfiltered page remains a useful fallback if the month navigation
    # markup changes, and duplicates are removed after parsing.
    return sorted(urls) or [SCHEDULE_URL]


def listing_record(node):
    link = node.select_one('.performance__title a[href]')
    title_node = node.select_one('.performance__title [itemprop="name"]')
    start_node = node.select_one('meta[itemprop="startDate"][content]')
    venue_node = node.select_one('.performance__location')
    if not all((link, title_node, start_node, venue_node)):
        return None

    title = clean_text(title_node)
    event_date, event_time = parse_start(start_node.get('content'))
    venue = clean_venue(venue_node)
    url = urljoin(SOURCE_URL, link.get('href'))
    if not all((title, event_date, url, venue)):
        return None

    listing_parts = [
        clean_text(item)
        for item in node.select('.performance__kicker, .performance__infos')
    ]
    listing_parts = [part for part in listing_parts if part]
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        # This is the institutional Essen calendar. Its named partner venues
        # (for example Folkwang and Neue Musik Zentrale) are also in Essen.
        'city': CITY,
        'country_code': 'DE',
        'description': '\n\n'.join(listing_parts) or None,
    }


def detail_description(session, record):
    soup = get_soup(session, record['url'])
    parts = []

    for selector in (
        '.productionhead__kicker',
        '.productionhead__infos',
        '.productionworkinformation',
        '.page-outer--richtext .richtext',
    ):
        for node in soup.select(selector):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)

    listing_text = record.get('description')
    if listing_text and listing_text not in parts:
        parts.insert(0, listing_text)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = make_session()
    records = {}
    for calendar_url in calendar_urls(session):
        soup = get_soup(session, calendar_url)
        for node in soup.select('.performance[itemtype$="/Event"]'):
            record = listing_record(node)
            if record:
                key = (
                    record['url'], record['date'], record['time_from'],
                    record['venue'],
                )
                records[key] = record

    # Each dated URL ends in a performance id, while every performance of the
    # same production has identical programme and description content. Fetch
    # that content only once per production slug.
    production_groups = {}
    for record in records.values():
        production_key = record['url'].rsplit('/', 2)[0]
        production_groups.setdefault(production_key, []).append(record)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, group[0]): group
            for group in production_groups.values()
        }
        for future in as_completed(futures):
            group = futures[future]
            try:
                description = future.result()
                for record in group:
                    record['description'] = description
            except (requests.RequestException, ValueError) as error:
                record = group[0]
                log_message(
                    'Failed to scrape Theater Essen event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(records.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['venue'], item['title'],
        item['url'],
    ))


class TheaterEssenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_essen_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterEssenDeCrawler().run()


if __name__ == '__main__':
    main()
