import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.landestheater-detmold.de/de/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programm')
SOURCE = 'Landestheater Detmold'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'maerz': 3,
    'märz': 3,
    'april': 4,
    'mai': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'dezember': 12,
}

FOREIGN_CITY_COUNTRIES = {
    'winterthur': 'CH',
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def date_from_block(item, anchor):
    day_node = item.select_one('.event-list-date .day')
    month_node = item.select_one('.event-list-date .month')
    if not day_node or not month_node:
        return None
    try:
        day = int(clean_text(day_node))
        month = MONTHS[clean_text(month_node).casefold()]
        year = anchor.year
        # A page spans only a few weeks, but can cross New Year's Day.
        if month < anchor.month - 6:
            year += 1
        elif month > anchor.month + 6:
            year -= 1
        return date(year, month, day).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def split_location(value):
    """The calendar consistently publishes locations as ``city, venue``."""
    location = clean_text(value)
    if ',' not in location:
        return None, None
    city, venue = (part.strip() for part in location.split(',', 1))
    if not city or not venue or city.casefold() == venue.casefold():
        return None, None
    return city, venue


def parse_listing_page(soup, anchor):
    records = []
    for date_item in soup.select('li.event-list__item'):
        event_date = date_from_block(date_item, anchor)
        if not event_date:
            continue
        for item in date_item.select('li.event-list__plays--item'):
            link = item.select_one('.event-list__plays--info > a[href]')
            title_node = item.select_one('.title-container .title')
            location_node = item.select_one('.time .location')
            if not link or not title_node or not location_node:
                continue
            city, venue = split_location(location_node)
            if not city or not venue:
                continue
            title = clean_text(title_node)
            url = urljoin(SOURCE_URL, link.get('href'))
            if not title or not url:
                continue
            time_node = item.select_one('.time')
            time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', clean_text(time_node))
            summary_node = item.select_one('.copy')
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_match.group(0) if time_match else None,
                'venue': venue,
                'city': city,
                'country_code': FOREIGN_CITY_COUNTRIES.get(city.casefold(), 'DE'),
                'description': clean_text(summary_node) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def page_anchor(url):
    match = re.search(r'/(\d{2})-(\d{2})-(\d{4})/?$', urlparse(url).path)
    if not match:
        return date.today()
    day, month, year = map(int, match.groups())
    return date(year, month, day)


def detail_description(record):
    response = requests.get(record['url'], headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    parts = []
    subtitle = soup.select_one('.hero-copy .subtitle')
    body = soup.select_one('#play .text')
    for node in (subtitle, body):
        value = clean_text(node, separator='\n')
        if value and value not in parts:
            parts.append(value)
    listing_summary = record.get('description')
    if listing_summary and listing_summary not in parts:
        parts.insert(0, listing_summary)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_date = date.today()
    next_url = f'{PROGRAM_URL}/{first_date:%d-%m-%Y}'
    visited_pages = set()
    records_by_key = {}

    while next_url and next_url not in visited_pages:
        visited_pages.add(next_url)
        try:
            soup = get_soup(session, next_url)
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            # At the end of the announced season the site's final "more"
            # link points at an empty page which responds with 500, not 404.
            if records_by_key and status == 500:
                log_message(
                    'Reached broken terminal programme page',
                    event='crawler_pagination_ended',
                    level='warning',
                    url=next_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                break
            raise
        anchor = page_anchor(next_url)
        for record in parse_listing_page(soup, anchor):
            key = (record['url'], record['date'], record['time_from'], record['venue'])
            records_by_key[key] = record
        more = soup.select_one('a.load-more-events[href]')
        next_url = urljoin(SOURCE_URL, more.get('href')) if more else None

    records = list(records_by_key.values())
    records_by_production = {}
    for record in records:
        # The last path component identifies an occurrence. The descriptive
        # production copy is shared by all occurrences of the same work.
        production_key = record['url'].rsplit('/', 1)[0]
        records_by_production.setdefault(production_key, []).append(record)
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, grouped_records[0]): grouped_records
            for grouped_records in records_by_production.values()
        }
        for future in as_completed(futures):
            grouped_records = futures[future]
            try:
                description = future.result()
                for record in grouped_records:
                    record['description'] = description
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=grouped_records[0]['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class LandestheaterDetmoldDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='landestheater_detmold_de',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LandestheaterDetmoldDeCrawler().run()


if __name__ == '__main__':
    main()
