import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://reisopera.nl/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programma-index')
SOURCE = 'Nederlandse Reisopera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
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


def production_links(session):
    soup = get_soup(session, PROGRAM_URL)
    links = set()
    for anchor in soup.select('main a[href]'):
        url = urljoin(SOURCE_URL, anchor.get('href'))
        if re.fullmatch(r'/programma/[^/]+', urlparse(url).path.rstrip('/')):
            links.add(url)
    return sorted(links)


def parse_date(value):
    match = re.fullmatch(
        r'\s*([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th),\s*(\d{4})\s*',
        value or '',
    )
    if not match:
        return None
    month_name, day, year = match.groups()
    month = MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def production_description(soup):
    parts = []
    for node in soup.select('.intro, .pdf-link-content'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    if not parts:
        meta = soup.select_one('meta[name="description"]')
        fallback = clean_text(meta.get('content')) if meta else ''
        if fallback:
            parts.append(fallback)
    return '\n\n'.join(parts) or None


def country_for_city(city):
    # Reisopera is a Dutch touring company. The programme currently also
    # contains an explicitly advertised London performance.
    if city.casefold() in {'london', 'londen'}:
        return 'GB'
    return 'NL'


def parse_occurrence(anchor, title, production_url, description):
    date_node = anchor.select_one('time[datetime]')
    performance_date = parse_date(date_node.get('datetime')) if date_node else None
    times = anchor.select('time[time]')
    time_from = clean_text(times[0].get('time')) if times else None

    venue_node = anchor.select_one('h2.h6')
    city_node = venue_node.find_next_sibling('small') if venue_node else None
    venue = clean_text(venue_node)
    city = clean_text(city_node)
    if not performance_date or not title or not venue or not city:
        return None
    if time_from and not re.fullmatch(r'[0-2]\d:[0-5]\d', time_from):
        time_from = None

    return {
        'title': title,
        'date': performance_date,
        'url': production_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_for_city(city),
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_production(session, url):
    soup = get_soup(session, url)
    heading = soup.select_one('main h1')
    title = clean_text(heading)
    description = production_description(soup)
    records = []
    for date_node in soup.select('.tour time[datetime]'):
        occurrence = date_node.find_parent(
            lambda tag: tag.name in ('a', 'div')
            and 'flex-wrap' in tag.get('class', [])
        )
        if not occurrence:
            continue
        record = parse_occurrence(occurrence, title, url, description)
        if record:
            records.append(record)
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = production_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(scrape_production, session, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Reisopera production',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'],
            record['venue'], record['url'],
        ),
    )


class ReisoperaNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='reisopera_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ReisoperaNlCrawler().run()


if __name__ == '__main__':
    main()
