import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://slosymphony.org/'
SOURCE = 'San Luis Obispo Symphony'
SITEMAP_URL = urljoin(SOURCE_URL, 'page-sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

LISTING_PATH_RE = re.compile(
    r'^/(?:calendar-\d{4}-\d{4}|\d{4}-\d{4}-season)/$'
)
EVENT_PATH_RE = re.compile(
    r'^/(?:calendar(?:-\d{4}-\d{4})?|\d{4}-\d{4}-season)/[^/]+/$'
)
DATE_RE = re.compile(
    r'(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s*,?\s*)?'
    r'([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(f'{month} {day} {year}', '%b %d %Y').date().isoformat()
        except ValueError:
            return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour)
    if not 1 <= hour <= 12:
        return None
    hour = hour % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def event_location(value):
    text = clean_text(value)
    if re.search(r'Avila Beach Golf Resort', text, re.IGNORECASE):
        return 'Avila Beach Golf Resort', 'Avila Beach'
    if re.search(r'(?:San Luis Obispo\s+)?Performing Arts Center(?:\s+San Luis Obispo)?', text, re.IGNORECASE):
        return 'Performing Arts Center', 'San Luis Obispo'
    # The orchestra's numbered Classics concerts are held at its home PAC;
    # old calendar cards sometimes omit the location after the performance.
    return 'Performing Arts Center', 'San Luis Obispo'


def listing_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.text, 'xml')
    urls = []
    for node in sitemap.select('loc'):
        url = clean_text(node.get_text())
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc and LISTING_PATH_RE.fullmatch(
            urlparse(url).path
        ):
            urls.append(url)
    return sorted(set(urls))


def event_cards(soup, listing_url):
    main = soup.select_one('main') or soup
    seen = set()
    for link in main.select('a[href]'):
        url = urljoin(listing_url, link.get('href')).split('#', 1)[0]
        if url in seen or urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
            continue
        if not EVENT_PATH_RE.fullmatch(urlparse(url).path):
            continue
        card = link.find_parent('div', class_=lambda value: value and 'kt-row-column-wrap' in value)
        if not card:
            continue
        card_text = clean_text(card.get_text(' ', strip=True))
        event_date = parse_date(card_text)
        if not event_date:
            continue
        title_node = card.select_one('h1, h2, h3, h4')
        title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
        if not title:
            continue
        seen.add(url)
        yield title, event_date, parse_time(card_text), event_location(card_text), url


def detail_content(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_request_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None, None
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('main')
    description = clean_text(main.get_text('\n', strip=True)) if main else None
    return description, parse_time(description)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    seen = set()

    for listing_url in listing_urls(session):
        response = session.get(listing_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for title, event_date, time_from, location, url in event_cards(soup, listing_url):
            venue, city = location
            key = (title.casefold(), event_date, time_from, venue.casefold())
            if key in seen:
                continue
            seen.add(key)
            description, detail_time = detail_content(session, url)
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from or detail_time,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SloSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='slosymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
        return scrape_concerts()


def main():
    SloSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
