import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theaterheidelberg.de/en'
CALENDAR_URL = f'{SOURCE_URL}/kalender'
SOURCE = 'Theater und Orchester Heidelberg'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,de;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def resolve_city(venue):
    value = venue.lower()
    # The calendar includes the Winter in Schwetzingen festival as well as
    # Heidelberg performances. Explicit place names always override the
    # organisation's well-supported Heidelberg default.
    if 'schwetzingen' in value:
        return 'Schwetzingen'
    if 'eppelheim' in value:
        return 'Eppelheim'
    postal_city = re.search(r'\b\d{5}\s+([A-ZÄÖÜ][\wÄÖÜäöüß.-]+(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß.-]+)*)', venue)
    if postal_city:
        return postal_city.group(1)
    if 'heidelberg' in value:
        return 'Heidelberg'
    return 'Heidelberg'


def listing_record(section, item):
    link = item.select_one('a.calendar-item__title[href]')
    title_node = item.select_one('.calendar-item__title')
    venue_node = item.select_one('.calendar-item__stages')
    time_node = item.select_one('.calendar-item__time')
    raw_date = section.get('aria-label') or ''
    title = clean_text(title_node)
    venue = clean_text(venue_node)
    url = urljoin(SOURCE_URL, link.get('href')) if link else ''
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        return None
    if not title or not venue or not url:
        return None

    raw_time = clean_text(time_node)
    time_match = re.match(r'(\d{1,2}):(\d{2})', raw_time)
    subtitle = clean_text(item.select_one('.calendar-item__subtitle'))
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': (
            f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            if time_match else None
        ),
        'venue': venue,
        'city': resolve_city(venue),
        'country_code': 'DE',
        'description': subtitle or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    for block in soup.select('main .block--text .block-text'):
        value = clean_text(block)
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, CALENDAR_URL)
    records = []
    for section in soup.select('section.calendar-section[aria-label]'):
        for item in section.select('.calendar-item'):
            record = listing_record(section, item)
            if record:
                records.append(record)

    descriptions = {}
    urls = {record['url'] for record in records}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, url): url for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record['url'])
        summary = record['description']
        if detail and summary and summary not in detail:
            record['description'] = f'{summary}\n\n{detail}'
        elif detail:
            record['description'] = detail

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class TheaterheidelbergDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theaterheidelberg_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
    TheaterheidelbergDeCrawler().run()


if __name__ == '__main__':
    main()
