import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ashevillechambermusic.org/'
SOURCE = 'Asheville Chamber Music Series'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
EVENT_PATH_PREFIX = '/2026/27-season/'
LOCAL_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.8',
}


def clean_html(value):
    if not value:
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('script, style'):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def parse_item(item):
    title = html.unescape(item.get('title', '')).strip()
    event_path = item.get('fullUrl', '')
    start_timestamp = item.get('startDate')
    location = item.get('location') or {}
    venue = html.unescape(location.get('addressTitle', '')).strip()
    address_line = html.unescape(location.get('addressLine2', '')).strip()
    city = address_line.split(',', 1)[0].strip()

    if not all((title, event_path, start_timestamp, venue, city)):
        return None

    try:
        starts_at = datetime.fromtimestamp(start_timestamp / 1000, LOCAL_TIMEZONE)
    except (OSError, OverflowError, TypeError, ValueError):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(SOURCE_URL, event_path),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_html(item.get('body')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AshevilleChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ashevillechambermusic_org',
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

    def _event_urls(self):
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'xml')
        urls = []
        for location in soup.select('url > loc'):
            url = location.get_text(strip=True)
            parsed = urlparse(url)
            if parsed.netloc == 'www.ashevillechambermusic.org' and parsed.path.startswith(
                EVENT_PATH_PREFIX
            ):
                urls.append(url)
        return sorted(set(urls))

    @staticmethod
    def _fetch_item(url):
        response = requests.get(
            url,
            params={'format': 'json'},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        return response.json().get('item')

    def scrape(self):
        try:
            event_urls = self._event_urls()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Asheville Chamber Music sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._fetch_item, url): url for url in event_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    item = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Asheville Chamber Music event',
                        event='crawler_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if item:
                    record = parse_item(item)
                    if record:
                        records.append(record)

        if event_urls and not records:
            raise ValueError('No valid event records could be parsed')

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    AshevilleChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
