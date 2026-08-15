import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chambermusichouston.org/'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-concerts-1.xml'
SOURCE = 'Chamber Music Houston'
CITY = 'Houston'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    normalized = re.sub(r'\s+', ' ', match.group(1).upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_concert_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    header = soup.select_one('.concert-list-single-right')
    if not header:
        return None

    title = clean_text(header.select_one('.concert-list-title'))
    event_info = clean_text(header.select_one('.concert-list-info'))
    venue = clean_text(header.select_one('.concert-list-program'))
    event_date = parse_date(event_info)

    # CMH's streaming pages reuse the concert post type but contain explanatory
    # prose instead of a venue. Concrete hall concerts use the pipe-separated
    # date/time header and a short venue field.
    if not title or not event_date or '|' not in event_info or not venue or len(venue) > 160:
        return None

    description_parts = []
    for selector in ('.concert-list-single-detail-top', '.concert-list-single-long'):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event_info),
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.content, 'xml')
    urls = [
        clean_text(node)
        for node in sitemap.find_all('loc')
        if '/concerts/' in clean_text(node)
    ]

    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_concert_page(response.text, url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concrete concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ChamberMusicHoustonOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusichouston_org',
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
    ChamberMusicHoustonOrgCrawler().run()


if __name__ == '__main__':
    main()
