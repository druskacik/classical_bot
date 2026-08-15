import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://artisnaples.org/'
EVENTS_URL = f'{SOURCE_URL}events/'
SOURCE = 'Artis—Naples'
CITY = 'Naples'
COUNTRY_CODE = 'US'
TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def is_detail_url(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip('/')
    return (
        parsed.netloc in {'artisnaples.org', 'www.artisnaples.org'}
        and path.startswith('/events/')
        and not re.fullmatch(r'/events/P\d+', path)
    )


def listing_urls(session):
    """Follow the public 50-item offset pagination and collect detail URLs."""
    page_url = EVENTS_URL
    seen_pages = set()
    urls = []
    seen_urls = set()

    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        soup = get_page(session, page_url)

        for card in soup.select('article.js-isotope-entry a[href]'):
            url = urljoin(page_url, card.get('href'))
            if is_detail_url(url) and url not in seen_urls:
                seen_urls.add(url)
                urls.append(url)

        next_url = None
        for link in soup.select('a[href]'):
            href = urljoin(page_url, link.get('href'))
            if re.fullmatch(r'https://(?:www\.)?artisnaples\.org/events/P\d+/?', href):
                offset = int(re.search(r'/P(\d+)', href).group(1))
                if href not in seen_pages and (next_url is None or offset < next_url[0]):
                    next_url = (offset, href)
        page_url = next_url[1] if next_url else None

    return urls


def parse_occurrences(soup):
    occurrences = []
    for select in soup.select('select.js-ticket-date-select'):
        venue = clean_text(select.get('data-location'))
        if not venue:
            continue
        for option in select.select('option[value]'):
            try:
                moment = datetime.fromtimestamp(int(option['value']), TIME_ZONE)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            occurrences.append((moment.date().isoformat(), moment.strftime('%H:%M'), venue))
    return occurrences


def parse_detail(soup, url):
    title_node = soup.find('h1')
    title = re.sub(r'\s+', ' ', clean_text(title_node)).strip()
    if not title:
        return []

    description_node = soup.select_one('article.tab-content#tab1, article.tab-content')
    description = clean_text(description_node) or None

    records = []
    for event_date, time_from, venue in parse_occurrences(soup):
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_events():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_page, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_detail(future.result(), url))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No dated event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class ArtisNaplesOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='artisnaples_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
        return scrape_events()


def main():
    ArtisNaplesOrgCrawler().run()


if __name__ == '__main__':
    main()
