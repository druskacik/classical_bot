import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dmsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-events/')
SOURCE = 'Des Moines Symphony'
COUNTRY_CODE = 'US'
DEFAULT_CITY = 'Des Moines'
ARCHIVE_START_YEAR = 2019

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
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def archive_urls():
    today = date.today()
    return [
        f'{LISTING_URL}{year:04d}-{month:02d}/'
        for year in range(ARCHIVE_START_YEAR, today.year + 1)
        for month in range(1, 13)
        if (year, month) <= (today.year, today.month)
    ]


def detail_links(soup):
    return {
        urljoin(LISTING_URL, node.get('href'))
        for node in soup.select('nav.event-list .title a[href]')
        if '/concerts-events/' in node.get('href', '')
    }


def discover_event_urls():
    urls = set()
    discovery_pages = archive_urls()

    # The paginated main listing supplies future events. Historical events remain
    # available through the site's first-party YYYY-MM archive routes.
    first_page = get_soup(LISTING_URL)
    urls.update(detail_links(first_page))
    page_numbers = [
        int(option.get_text(strip=True))
        for option in first_page.select('select[name="page"] option')
        if option.get_text(strip=True).isdigit()
    ]
    discovery_pages.extend(
        f'{LISTING_URL}?page={page}' for page in range(2, max(page_numbers, default=1) + 1)
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_url = {executor.submit(get_soup, url): url for url in discovery_pages}
        for future in as_completed(future_to_url):
            page_url = future_to_url[future]
            try:
                urls.update(detail_links(future.result()))
            except requests.RequestException as error:
                log_message(
                    'Event discovery page failed',
                    event='crawler_page_failed',
                    level='warning',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(urls)


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*[AP]M)\b', clean_text(value), re.I)
    if not match:
        return None
    normalized = re.sub(r'\s*([AP]M)$', r' \1', match.group(1).upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_description(soup):
    sections = []
    for selector in ('.event-desc-module .module-text', '.program-module'):
        node = soup.select_one(selector)
        text = clean_text(node)
        if text and text not in sections:
            sections.append(text)
    return '\n\n'.join(sections) or None


def parse_detail(url):
    soup = get_soup(url)
    title_node = soup.select_one('.event-desc-module .module-text h2') or soup.select_one('.sub-feature h1')
    title = clean_text(title_node)
    description = event_description(soup)
    records = []

    for instance in soup.select('.show-instances > li'):
        event_date = parse_date(instance.select_one('.sl-date'))
        time_node = instance.select_one('.time')
        venue_node = time_node.select_one('a') if time_node else None
        venue = clean_text(venue_node)
        if not title or not event_date or not venue:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(time_node),
            'venue': venue,
            'city': DEFAULT_CITY,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts():
    event_urls = discover_event_urls()
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_url = {executor.submit(parse_detail, url): url for url in event_urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Event detail page failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    unique = {
        (item['url'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class DmSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dmsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    DmSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
