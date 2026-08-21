import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fabriziopaterlini.com/'
ARCHIVE_URL = urljoin(SOURCE_URL, '2024-china-tour')
SOURCE = 'Fabrizio Paterlini'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(clean_text(value), '%I:%M%p').strftime('%H:%M')
    except ValueError:
        return None


def split_location(value):
    parts = [part.strip() for part in clean_text(value).rsplit(',', 1)]
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def parse_detail(session, url):
    soup = get_soup(session, url)
    paragraphs = []
    for node in soup.select('.event-notes'):
        text = clean_text(node)
        if text:
            paragraphs.append(text)
    return '\n\n'.join(dict.fromkeys(paragraphs)) or None


def parse_archive_row(row, session):
    title_node = row.select_one('td.event-name .text:not(.text-tertiary)')
    date_node = row.select_one('td.event-date .date-long time.from .date')
    start_node = row.select_one('td.event-date .date-long time.from .time')
    end_node = row.select_one('td.event-date .date-long time.to .time')
    location_node = row.select_one('td.event-location .text')
    link = row.select_one('a.event_details[href]')
    if not all((title_node, date_node, location_node, link)):
        return None

    title = clean_text(title_node)
    location = split_location(location_node)
    try:
        event_date = datetime.strptime(clean_text(date_node), '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return None
    if not title or not location:
        return None

    venue, city = location
    url = urljoin(SOURCE_URL, link['href'])
    try:
        description = parse_detail(session, url)
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Fabrizio Paterlini event detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        description = None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(start_node),
        'time_to': parse_time(end_node),
        'venue': venue,
        'city': city,
        'country_code': 'CN',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FabrizioPaterliniComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fabriziopaterlini_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            soup = get_soup(session, ARCHIVE_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Fabrizio Paterlini concert archive',
                event='crawler_fetch_failed',
                level='error',
                url=ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for row in soup.select('table tr'):
            record = parse_archive_row(row, session)
            if record:
                records.append(record)
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    FabrizioPaterliniComCrawler().run()


if __name__ == '__main__':
    main()
