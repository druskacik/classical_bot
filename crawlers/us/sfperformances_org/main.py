import copy
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sfperformances.org/'
SOURCE = 'San Francisco Performances'
CALENDAR_URLS = (
    urljoin(SOURCE_URL, 'performances/performance-calendar.html'),
    urljoin(SOURCE_URL, 'performances/performance-calendar-past.html'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(node, separator=' '):
    if node is None:
        return ''
    text = node.get_text(separator, strip=True) if hasattr(node, 'get_text') else str(node)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):(\d{2})\s*([ap])m\b', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_date(value):
    date_text = re.split(r'\s*\|\s*', value, maxsplit=1)[0].strip()
    try:
        return datetime.strptime(date_text, '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return None


def detail_urls(calendar_html):
    soup = BeautifulSoup(calendar_html, 'html.parser')
    urls = []
    for card in soup.select('.listing-container-new[data-link]'):
        path = card.get('data-link', '').strip().strip('/')
        if path:
            urls.append(urljoin(SOURCE_URL, f'performances/{path}.html'))
    return urls


def structured_location(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.get_text()
        location = re.search(
            r'"location"\s*:\s*\{.*?"name"\s*:\s*"([^"]+)"', text, re.DOTALL
        )
        city = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', text)
        if location or city:
            return (
                location.group(1).strip() if location else None,
                city.group(1).strip() if city else None,
            )
    return None, None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main.perfpage-new')
    if main is None:
        return None

    title = clean_text(main.find('h1'))
    date_node = main.select_one('.perfinfo-date')
    date_text = clean_text(date_node)
    event_date = parse_date(date_text)

    venue_node = main.select_one('.perfinfo-venue-new')
    if venue_node is not None:
        venue_node = copy.copy(venue_node)
        for extra in venue_node.select('.icon-venue-info'):
            extra.decompose()
    venue = clean_text(venue_node)
    structured_venue, city = structured_location(soup)
    venue = venue or structured_venue

    description_parts = []
    for selector in ('.perfabout-content-new', '.perfprogram-content-new'):
        section = main.select_one(selector)
        text = clean_text(section, separator='\n')
        if text:
            description_parts.append(text)

    if not all((title, event_date, venue, city, url)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_text),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SfperformancesOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfperformances_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = []
        for calendar_url in CALENDAR_URLS:
            try:
                response = session.get(calendar_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch San Francisco Performances calendar',
                    event='crawler_fetch_failed',
                    level='error',
                    url=calendar_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            urls.extend(detail_urls(response.content))

        urls = list(dict.fromkeys(urls))
        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch San Francisco Performances detail page',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_detail(response.content, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete San Francisco Performances event',
                    event='crawler_record_skipped',
                    level='warning',
                    url=url,
                )

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    return SfperformancesOrgCrawler().run()


if __name__ == '__main__':
    main()
