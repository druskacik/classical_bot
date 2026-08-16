import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://music.uni.edu/'
LISTING_URL = 'https://calendar.uni.edu/publish/music'
SOURCE = 'University of Northern Iowa School of Music'
CITY = 'Cedar Falls'
DEFAULT_VENUE = 'University of Northern Iowa'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def date_from_url(url):
    match = re.search(r'/events/(\d{4}-\d{2}-\d{2})/', url)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}(?::\d{2})?)\s*([ap]m)\b', value, re.I)
    if not match:
        return None
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(''.join(match.groups()), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def listing_items(html):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for article in soup.select('main .view-content article'):
        link = article.select_one('a.event-link[href*="/events/"]')
        if not link:
            continue
        url = urljoin(LISTING_URL, link.get('href'))
        title = clean_text(link)
        event_date = date_from_url(url)
        if not title or not event_date:
            continue
        items.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(clean_text(article.select_one('p'))),
        })

    next_link = soup.select_one('nav.pager a[rel="next"]')
    next_url = urljoin(LISTING_URL, next_link.get('href')) if next_link else None
    return items, next_url


def detail_fields(html):
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one('article.node--type-event')
    if not node:
        return None, None

    details = node.select_one('.node__content .details p')
    lines = [clean_text(value) for value in details.stripped_strings] if details else []
    lines = [value for value in lines if value]
    venue = None
    for line in reversed(lines):
        if not re.search(r'\d{1,2}(?::\d{2})?\s*[ap]m|^[A-Z][a-z]{2},\s', line, re.I):
            venue = line
            break

    body = node.select_one('.field--name-body')
    description = clean_text(body) or None
    return venue, description


def fetch_detail(session, item):
    try:
        response = session.get(item['url'], timeout=45)
        response.raise_for_status()
        venue, description = detail_fields(response.text)
    except requests.RequestException as error:
        log_message(
            'Event detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=item['url'],
            error_type=type(error).__name__,
            error_message=str(error),
        )
        venue, description = None, None

    return {
        **item,
        'venue': venue or DEFAULT_VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    items = []
    page_url = LISTING_URL
    seen_pages = set()
    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        page_items, page_url = listing_items(response.text)
        items.extend(page_items)

    if not items:
        log_message(
            'No event records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
        return []

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_detail, session, item) for item in items]
        for future in as_completed(futures):
            records.append(future.result())

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MusicUniEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='music_uni_edu',
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
    MusicUniEduCrawler().run()


if __name__ == '__main__':
    main()
