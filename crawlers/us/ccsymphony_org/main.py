import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ccsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'tickets')
SOURCE = 'Corpus Christi Symphony Orchestra'
CITY = 'Corpus Christi'
VENUE = 'H-E-B Performance Hall at the Performing Arts Center at Texas A&M University-Corpus Christi'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[AP]M)(?:\s+[A-Z]{2,4})?\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(match.group(1).upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def concert_links(soup):
    links = []
    seen = set()
    for link in soup.select('a[href]'):
        url = urljoin(LISTING_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc.lower() not in {'ccsymphony.org', 'www.ccsymphony.org'}:
            continue
        if not link.find_parent(class_=re.compile(r'(?:^|-)schedule(?:-|$)|tickets_1', re.I)):
            continue
        url = url.split('#', 1)[0]
        if url.rstrip('/') == LISTING_URL.rstrip('/') or url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links


def parse_concert(soup, url):
    page_text = clean_text(soup)
    event_date = parse_date(page_text)
    time_from = parse_time(page_text)

    titles = [clean_text(item) for item in soup.select('h1[class*="concert-details"]')]
    if not any(titles):
        titles = [clean_text(item) for item in soup.select('h2[class*="concert-details"]')]
    title = max(titles, key=len, default='')

    descriptions = [
        clean_text(item)
        for item in soup.select('p[class*="concert-details"]')
        if clean_text(item)
    ]
    description = max(descriptions, key=len, default=None)

    venue_evidence = description and re.search(
        r'H-E-B Performance Hall.*?Texas A&M University\s*-?\s*Corpus Christi',
        description,
        re.IGNORECASE | re.DOTALL,
    )
    if not title or not event_date or not time_from or not venue_evidence:
        log_message(
            'Skipping incomplete concert page',
            event='crawler_record_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(event_date),
            has_time=bool(time_from),
            has_venue=bool(venue_evidence),
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    links = concert_links(BeautifulSoup(response.text, 'html.parser'))

    records = []
    for url in links:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_concert(BeautifulSoup(response.text, 'html.parser'), url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not links or not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            discovered_link_count=len(links),
            record_count=len(records),
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class CcSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ccsymphony_org',
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
    CcSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
