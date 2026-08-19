import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.philharmonicsociety.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-tickets/calendar-of-events')
SOURCE = 'Philharmonic Society of Orange County'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})'
    r'(?:\s+(\d{1,2}(?::\d{2})?\s*[AP]M))?',
    re.IGNORECASE,
)
CITY_RE = re.compile(r'([^\n,]+),\s*CA\s+\d{5}(?:-\d{4})?\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None

    time_from = None
    if match.group(2):
        raw_time = re.sub(r'\s+', '', match.group(2)).upper()
        for pattern in ('%I:%M%p', '%I%p'):
            try:
                time_from = datetime.strptime(raw_time, pattern).strftime('%H:%M')
                break
            except ValueError:
                pass
    return event_date, time_from


def listing_urls(session):
    urls = []
    seen = set()
    page = 1
    while True:
        response = session.get(LISTING_URL, params={'p': page}, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = []
        for article in soup.select('main article'):
            link = article.select_one('a[href*="/calendar-of-events/"]')
            if link:
                url = urljoin(LISTING_URL, link.get('href'))
                if url not in seen:
                    seen.add(url)
                    page_urls.append(url)
        if not page_urls:
            break
        urls.extend(page_urls)
        next_link = soup.select_one(f'a[href*="p={page + 1}"]')
        if not next_link:
            break
        page += 1
    return urls


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('h1')
    details = soup.select_one('.event-details .main-block')
    if not title_node or not details:
        return None

    title = clean_text(title_node)
    detail_items = details.select('li')
    if len(detail_items) < 2:
        return None
    event_date, time_from = parse_date_time(detail_items[0])

    location = detail_items[1]
    venue_node = location.find('strong')
    address_node = location.find('address')
    venue = re.sub(r'\s+', ' ', clean_text(venue_node)).strip()
    address = clean_text(address_node)
    city_match = CITY_RE.search(address)
    city = city_match.group(1).strip() if city_match else ''
    if not title or not event_date or not venue or not city:
        return None

    description_node = soup.select_one('.event-details .details-wrap')
    description = clean_text(description_node) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
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
    urls = listing_urls(session)
    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_detail(response.text, url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No valid event records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PhilharmonicSocietyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='philharmonicsociety_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    PhilharmonicSocietyOrgCrawler().run()


if __name__ == '__main__':
    main()
