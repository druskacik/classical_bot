import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.adriansymphony.org/'
SOURCE = 'Adrian Symphony Orchestra'
LISTING_URLS = (
    urljoin(SOURCE_URL, 'concerts-list.html'),
    urljoin(SOURCE_URL, '2025/concerts-list.html'),
    urljoin(SOURCE_URL, '2024/concerts-list.html'),
    urljoin(SOURCE_URL, '2023/'),
    urljoin(SOURCE_URL, '2022/'),
    urljoin(SOURCE_URL, '2021/'),
    urljoin(SOURCE_URL, '2019/'),
)
SUMMER_URL = urljoin(SOURCE_URL, 'summer-concerts.html')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(node):
    if node is None:
        return ''
    text = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_datetime(text, default_year=None):
    match = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2})(?:,\s*(\d{4}))?\b.*?\b(\d{1,2}):(\d{2})\s*([AP]M)\b',
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    year = int(match.group(3) or default_year or 0)
    if not year:
        return None, None
    try:
        value = datetime.strptime(
            f'{match.group(1)} {match.group(2)} {year} {match.group(4)}:{match.group(5)} {match.group(6)}',
            '%B %d %Y %I:%M %p',
        )
    except ValueError:
        return None, None
    return value.date().isoformat(), value.strftime('%H:%M')


def listing_year(soup, url):
    heading = clean_text(soup.find(['h1', 'h2']))
    match = re.search(r'(20\d{2})', heading)
    if match:
        return int(match.group(1))
    match = re.search(r'/(20\d{2})/', url)
    return int(match.group(1)) if match else None


def event_year(date_text, season_year):
    explicit = re.search(r'\b(20\d{2})\b', date_text)
    if explicit:
        return int(explicit.group(1))
    month = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{1,2}\b',
        date_text,
        re.IGNORECASE,
    )
    if not month or season_year is None:
        return None
    month_number = datetime.strptime(month.group(1), '%B').month
    return season_year + (1 if month_number < 7 else 0)


def detail_description(soup):
    main = soup.select_one('.content') or soup.select_one('main') or soup
    paragraphs = []
    for node in main.find_all(['p', 'li']):
        text = clean_text(node)
        if not text or re.search(r'^(BUY|TICKET PRICES|Order Tickets|Join Us on Social)', text, re.I):
            continue
        if '$' in text or re.search(r'\b(Adult|Student|Premium Seating|General Seating)\b', text):
            continue
        paragraphs.append(text)
    return ' '.join(dict.fromkeys(paragraphs)) or None


def make_record(title, event_date, url, time_from, venue, description):
    if not all((title, event_date, url, venue)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Adrian',
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_listing(html, url, detail_pages=None):
    soup = BeautifulSoup(html, 'html.parser')
    season_year = listing_year(soup, url)
    records = []
    for card in soup.select('.list-item'):
        title = clean_text(card.find(['h2', 'h3', 'h4']))
        date_node = card.select_one('.date')
        venue = clean_text(card.select_one('.location'))
        date_text = clean_text(date_node)
        year = event_year(date_text, season_year)
        event_date, time_from = parse_datetime(date_text, year)
        link = card.select_one('.hero-image a[href], .button-container a[href]')
        event_url = urljoin(url, link['href']) if link else url
        description = None
        if detail_pages and event_url in detail_pages:
            description = detail_description(BeautifulSoup(detail_pages[event_url], 'html.parser'))
        if not description:
            programme = [
                clean_text(node) for node in card.find_all('p')
                if node is not date_node and 'location' not in (node.get('class') or [])
                and 'button' not in ' '.join(node.get('class') or [])
            ]
            description = ' '.join(item for item in programme if item) or None
        record = make_record(title, event_date, event_url, time_from, venue, description)
        if record:
            records.append(record)
    return records


def parse_summer_page(html, url=SUMMER_URL):
    soup = BeautifulSoup(html, 'html.parser')
    venue_heading = soup.find(
        lambda tag: tag.name in ('h2', 'h3', 'h4') and 'Holy Rosary Chapel' in clean_text(tag)
    )
    venue = clean_text(venue_heading) or 'Holy Rosary Chapel, Adrian Dominican Motherhouse campus'
    records = []
    for text_node in soup.find_all(string=re.compile(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{1,2},\s*20\d{2}', re.I
    )):
        container = text_node.parent
        date_text = clean_text(container)
        event_date, time_from = parse_datetime(date_text)
        heading = container.find_previous(
            lambda tag: tag.name == 'h2' and clean_text(tag) != 'Chamber Series Artists'
        )
        title = clean_text(heading)
        if not event_date or not title or title in {'Chamber Series Artists', 'CONCERT INFORMATION'}:
            continue
        description_parts = []
        for node in heading.find_all_next(['p', 'h2', 'h3', 'h4']):
            if node is not heading and node.name == 'h2':
                break
            value = clean_text(node)
            if value and value != date_text:
                description_parts.append(value)
        record = make_record(
            title, event_date, url, time_from, venue,
            ' '.join(dict.fromkeys(description_parts)) or None,
        )
        if record:
            records.append(record)
    return records


class AdrianSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='adriansymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        pages = {}
        for url in (*LISTING_URLS, SUMMER_URL):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                pages[url] = response.text
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Adrian Symphony page',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        detail_urls = set()
        for url in LISTING_URLS:
            if url not in pages:
                continue
            soup = BeautifulSoup(pages[url], 'html.parser')
            for link in soup.select('.list-item .hero-image a[href], .list-item .button-container a[href]'):
                detail_urls.add(urljoin(url, link['href']))

        detail_pages = {}

        def fetch_detail(url):
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            return response.text

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_pages[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Adrian Symphony concert detail',
                        event='crawler_detail_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for url in LISTING_URLS:
            if url in pages:
                records.extend(parse_listing(pages[url], url, detail_pages))
        if SUMMER_URL in pages:
            records.extend(parse_summer_page(pages[SUMMER_URL]))
        return records


def main():
    AdrianSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
