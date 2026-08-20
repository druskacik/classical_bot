import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.winterharbormusicfestival.org/'
SOURCE = 'Winter Harbor Music Festival'
EVENTS_URL = urljoin(SOURCE_URL, 'events.html')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
MONTHS = {
    name.lower(): number for number, name in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    ) if name
}
DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', re.IGNORECASE)
LOCATION_RE = re.compile(
    r'(?:@|\bat\s+)\s*([^\n,@]+?),\s*([^\n,]+?),\s*(?:ME|Maine)\b', re.IGNORECASE
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_occurrences(text, default_year=None):
    occurrences = []
    # An ampersand separates groups that have their own shared time. Within a
    # group, sites commonly list several dates followed by one time.
    for group in re.split(r'\s*&\s*', text):
        dates = list(DATE_RE.finditer(group))
        group_time = parse_time(group)
        for match in dates:
            year = int(match.group(3)) if match.group(3) else default_year
            if not year:
                continue
            try:
                day = date(year, MONTHS[match.group(1).lower()], int(match.group(2)))
            except ValueError:
                continue
            occurrences.append((day.isoformat(), group_time))
    return occurrences


def parse_location(text):
    match = LOCATION_RE.search(text)
    if not match:
        # Older product pages put the hall and city on a line after the date.
        match = re.search(r'(Hammond Hall),\s*(Winter Harbor),\s*(?:ME|Maine)\b', text, re.I)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def parse_product(html, url, default_year=None):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('#wsite-com-product-title'))
    description = clean_text(soup.select_one('#wsite-com-product-short-description'))
    location = parse_location(description)
    if not title or not description or not location:
        return []

    venue, city = location
    records = []
    for event_date, event_time in parse_occurrences(description, default_year):
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


class WinterHarborMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='winterharbormusicfestival_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events_response = get(session, EVENTS_URL)
            sitemap_response = get(session, SITEMAP_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Winter Harbor Music Festival index',
                event='crawler_fetch_failed', level='error', url=EVENTS_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        events_soup = BeautifulSoup(events_response.text, 'html.parser')
        year_match = re.search(r'\b(20\d{2})\s+Events\b', clean_text(events_soup))
        current_year = int(year_match.group(1)) if year_match else None
        current_urls = {
            urljoin(EVENTS_URL, link['href'])
            for link in events_soup.select('a.product-grid__item-overlay[href*="/store/p"]')
        }

        sitemap_soup = BeautifulSoup(sitemap_response.content, 'xml')
        product_urls = {
            loc.get_text(strip=True) for loc in sitemap_soup.select('loc')
            if '/store/p' in loc.get_text()
        }
        urls = sorted(product_urls | current_urls)
        records = []

        def fetch_product(url):
            response = requests.get(url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            return parse_product(response.text, url, current_year if url in current_urls else None)

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_product, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch festival product page',
                        event='crawler_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        if not records:
            log_message(
                'No dated festival performances found',
                event='crawler_empty_listing', level='warning',
                url=EVENTS_URL, record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    WinterHarborMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
