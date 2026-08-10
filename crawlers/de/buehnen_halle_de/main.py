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


SOURCE_URL = 'https://www.buehnen-halle.de/de/'
PROGRAM_URL = urljoin(SOURCE_URL, 'program')
SOURCE = 'Bühnen Halle'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=12,
        pool_maxsize=12,
        max_retries=Retry(
            total=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_location(value):
    venue = clean_text(value)
    if not venue:
        return None, None

    # The calendar qualifies touring locations with a trailing city, for
    # example "Kloster Chorin, Chorin". Unqualified stages belong to Halle.
    if ',' in venue:
        venue_name, city = (part.strip() for part in venue.rsplit(',', 1))
        if venue_name and city and re.fullmatch(r"[A-Za-zÄÖÜäöüß .\-()'/]+", city):
            # Two entries use the opposite "city, hall" convention.
            if city in {'Bach-Saal', 'Theater'} and ',' not in venue_name:
                return venue_name, city
            return city, venue_name
    return 'Halle (Saale)', venue


def parse_card(card):
    link = card.select_one('a.event-title[href]')
    title = clean_text(card.select_one('a.event-title'))
    date_text = clean_text(card.select_one('.event-information .date p'))
    location_text = clean_text(card.select_one('.event-information .location p'))
    if not link or not title or not date_text or not location_text:
        return None

    date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', date_text)
    time_match = re.search(r'(\d{2}:\d{2})\s*Uhr', date_text)
    if not date_match:
        return None
    try:
        date = datetime.strptime(date_match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None

    location = re.sub(r'^Ort\s*\n?', '', location_text, flags=re.IGNORECASE).strip()
    city, venue = parse_location(location)
    if not city or not venue:
        return None

    subtitle = clean_text(card.select_one('.event-subtitle'))
    genre = clean_text(card.select_one('.event-genre'))
    summary = '\n'.join(part for part in (subtitle, genre) if part) or None
    return {
        'title': title,
        'date': date,
        'url': urljoin(PROGRAM_URL, link['href']),
        'time_from': time_match.group(1) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': summary,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_listing(soup):
    return [record for card in soup.select('li.event-item') if (record := parse_card(card))]


def page_count(soup):
    pages = []
    for link in soup.select('a[href*="page="]'):
        match = re.search(r'[?&]page=(\d+)', link.get('href', ''))
        if match:
            pages.append(int(match.group(1)))
    return max(pages, default=1)


def detail_description(soup, existing=None):
    parts = []
    if existing:
        parts.append(existing)

    body = clean_text(soup.select_one('.readmore-text'))
    if body and body not in parts:
        parts.append(body)

    cast_items = []
    for item in soup.select('.cast-list .cast-item'):
        role = clean_text(item.select_one('.role'))
        value = clean_text(item.select_one('.names, .title'))
        line = ': '.join(part for part in (role, value) if part)
        if line and line not in cast_items:
            cast_items.append(line)
    if cast_items:
        parts.append('Besetzung und Programm\n' + '\n'.join(cast_items))
    return '\n\n'.join(parts) or None


def enrich_record(session, record):
    soup = get_soup(session, record['url'])
    record['description'] = detail_description(soup, record.get('description'))
    return record


def get_concerts():
    session = make_session()
    first_soup = get_soup(session, PROGRAM_URL)
    records = parse_listing(first_soup)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, session, PROGRAM_URL, {'page': page}): page
            for page in range(2, page_count(first_soup) + 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                records.extend(parse_listing(future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Bühnen Halle schedule page',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{PROGRAM_URL}?page={page}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {(item['url'], item['date'], item['time_from'], item['venue']): item for item in records}
    records = list(unique.values())

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(enrich_record, session, record): record for record in records}
        enriched = []
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Bühnen Halle event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                enriched.append(record)

    return sorted(enriched, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url']
    ))


class BuehnenHalleDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='buehnen_halle_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BuehnenHalleDeCrawler().run()


if __name__ == '__main__':
    main()
