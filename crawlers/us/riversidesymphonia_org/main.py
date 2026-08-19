import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://riversidesymphonia.org/'
SOURCE = 'Riverside Symphonia'
LISTING_URLS = (SOURCE_URL, urljoin(SOURCE_URL, 'buy-tickets/'))

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(\d{4})\b',
    re.IGNORECASE,
)
SHORT_DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,]?\s*'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?\b', re.IGNORECASE)

VENUES = (
    ('Tinicum Park', 'Erwinna'),
    ('South Hunterdon High School Performing Arts Center', 'Lambertville'),
    ('South Hunterdon Regional High School', 'Lambertville'),
    ('First Presbyterian Church of Lambertville', 'Lambertville'),
    ('Planetarium at the New Jersey State Museum', 'Trenton'),
)


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(text, title):
    match = DATE_RE.search(text)
    if match:
        value = ' '.join(match.groups())
    else:
        match = SHORT_DATE_RE.search(text)
        year = re.search(r'\b(20\d{2})\b', title)
        if not match or not year:
            return None
        value = f'{match.group(1)} {match.group(2)} {year.group(1)}'
    try:
        return datetime.strptime(value, '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_event_date(main, title):
    for node in main.select('p, h2, h3'):
        text = clean_text(node)
        if any(term in text.lower() for term in ('concert', 'performance', 'gathered')):
            event_date = parse_date(text, title)
            if event_date:
                return event_date
    return parse_date(clean_text(main), title)


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour, minute, period = match.groups()
    return datetime.strptime(
        f'{hour}:{minute or "00"} {period}m', '%I:%M %p'
    ).strftime('%H:%M')


def find_venue(text):
    lower_text = text.lower()
    for venue, city in VENUES:
        if venue.lower() in lower_text:
            return venue, city
    return None, None


def is_concrete_performance(text):
    lower_text = text.lower()
    performance_evidence = any(term in lower_text for term in (
        'concert', 'public performance', 'orchestra began', 'orchestra opened',
    ))
    return performance_evidence and ('riverside symphonia' in lower_text or 'orchestra' in lower_text)


def discover_detail_urls(session):
    urls = set()
    for listing_url in LISTING_URLS:
        response = session.get(listing_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.select('a[href]'):
            url = urljoin(listing_url, link.get('href')).split('#', 1)[0]
            parsed = urlparse(url)
            if parsed.netloc == urlparse(SOURCE_URL).netloc and parsed.path.startswith('/blog/'):
                urls.add(url)
    return sorted(urls)


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main')
    title_node = main.find('h1') if main else None
    title = clean_text(title_node)
    text = clean_text(main)
    if not title or not text or not is_concrete_performance(text):
        return None

    event_date = parse_event_date(main, title)
    venue, city = find_venue(text)
    if not event_date or not venue or not city:
        return None

    description = text.removeprefix(title).strip() or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(text),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in discover_detail_urls(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_detail(response.text, response.url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concrete concert pages found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class RiversideSymphoniaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='riversidesymphonia_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    RiversideSymphoniaOrgCrawler().run()


if __name__ == '__main__':
    main()
