import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://dekalbsymphony.org/'
SOURCE = 'DeKalb Symphony Orchestra'

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
    text = str(value).replace('\xa0', ' ').replace('\u200b', '').replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def concert_links(soup):
    host = urlparse(SOURCE_URL).netloc
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href')).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == host and parsed.path.startswith('/upcoming-concerts/'):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*([ap])m', clean_text(value), re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.casefold() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def city_from_address(value):
    # First-party addresses consistently end in "City, GA 12345".
    match = re.search(r',\s*([^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$', clean_text(value))
    return clean_text(match.group(1)) if match else None


def section_text(node):
    return clean_text(node.get_text('\n', strip=True)) if node else ''


def parse_detail_page(soup, url):
    header = soup.select_one('.event-header')
    if not header:
        return None

    title_node = header.select_one('h1')
    date_node = header.select_one('.heading-30')
    time_node = header.select_one('.heading-29')
    venue_node = header.select_one('.heading-28')
    address_node = header.select_one('.heading-27')

    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    event_date = parse_date(date_node.get_text(' ', strip=True) if date_node else '')
    time_from = parse_time(time_node.get_text(' ', strip=True) if time_node else '')
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
    city = city_from_address(address_node.get_text(' ', strip=True) if address_node else '')

    description_parts = []
    for selector in ('.program', '.full-description'):
        value = section_text(soup.select_one(selector))
        if value and value not in description_parts:
            description_parts.append(value)
    description = '\n\n'.join(description_parts) or None

    if not title or not event_date or not venue or not city:
        return None
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


class DekalbsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dekalbsymphony_org',
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
        session = make_session()
        try:
            index_soup = get_soup(session, SOURCE_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch DeKalb Symphony concert index',
                event='crawler_listing_request_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        urls = concert_links(index_soup)
        if not urls:
            raise ValueError('DeKalb Symphony index returned no concert detail links')

        records = []

        def fetch_detail(url):
            detail_session = make_session()
            try:
                return parse_detail_page(get_soup(detail_session, url), url)
            finally:
                detail_session.close()

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete DeKalb Symphony concert',
                            event='crawler_event_skipped',
                            level='warning',
                            url=url,
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch DeKalb Symphony concert detail',
                        event='crawler_detail_request_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        if not records:
            raise ValueError('DeKalb Symphony detail pages returned no valid concerts')
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    DekalbsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
