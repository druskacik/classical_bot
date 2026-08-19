import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sanbernardinosymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-events')
SOURCE = 'San Bernardino Symphony'
CITY = 'San Bernardino'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

PAGE_TITLE_RE = re.compile(
    r'^(?:[A-Z][a-z]{2,8})\s+(\d{1,2}),\s+(\d{4})\s+-\s+(.+?)'
    r'(?:\s*\|\s*SB Symphony)?$',
    re.I,
)
FULL_DATE_RE = re.compile(
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(?P<year>\d{4})',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?(?:\b|$)', re.I)
VENUE_PATTERNS = (
    r'California Theatre(?: of the Performing Arts)?',
    r'San Bernardino Valley College Auditorium',
    r'San Manuel Stadium',
    r'Arrowhead Events Center',
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(title, text):
    match = PAGE_TITLE_RE.match(clean_text(title))
    if match:
        month_text = clean_text(title).split()[0]
        candidate = f'{month_text} {match.group(1)} {match.group(2)}'
        for pattern in ('%b %d %Y', '%B %d %Y'):
            try:
                return datetime.strptime(candidate, pattern).date().isoformat()
            except ValueError:
                pass

    match = FULL_DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {match.group('year')}",
            '%B %d %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_venue(text):
    for pattern in VENUE_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match:
            return clean_text(match.group(0))
    return None


def event_urls(soup):
    urls = set()
    for link in soup.select('a[href]'):
        label = clean_text(link.get_text(' ', strip=True))
        if not re.match(r'^(?:[A-Z][a-z]{2,8})\s+\d{1,2},\s+\d{4}\s+-', label):
            continue
        url = urljoin(LISTING_URL, link.get('href'))
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
            urls.add(url.split('#', 1)[0])
    return sorted(urls)


def parse_event_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    page_title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    match = PAGE_TITLE_RE.match(page_title)
    title = clean_text(match.group(3)) if match else ''

    meta = soup.select_one('meta[name="description"]')
    summary = clean_text(meta.get('content')) if meta else ''
    main_node = soup.select_one('main')
    body = clean_text(main_node.get_text(' ', strip=True)) if main_node else ''
    evidence = clean_text(f'{summary} {body}')
    event_date = parse_date(page_title, evidence)
    venue = parse_venue(evidence)
    if not title or not event_date or not venue:
        return None

    description_parts = [part for part in (summary, body) if part]
    description = '\n\n'.join(dict.fromkeys(description_parts)) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(evidence),
        'venue': venue,
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
    urls = event_urls(BeautifulSoup(response.text, 'html.parser'))

    records = []
    for url in urls:
        try:
            detail = session.get(url, timeout=45)
            detail.raise_for_status()
            record = parse_event_page(url, detail.text)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event without required details',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Event page request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No complete concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SanBernardinoSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sanbernardinosymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SanBernardinoSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
