import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ums.org/'
SITEMAP_URL = 'https://ums.org/wp-sitemap-posts-performances-1.xml'
SOURCE = 'University Musical Society'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return ' '.join(text.replace('\xa0', ' ').split())


def get_response(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


def performance_urls():
    soup = BeautifulSoup(get_response(SITEMAP_URL).content, 'xml')
    return list(dict.fromkeys(node.get_text(strip=True) for node in soup.select('url > loc')))


def parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_page(url):
    soup = BeautifulSoup(get_response(url).text, 'html.parser')
    records = []

    # UMS emits one MusicEvent block for every concrete occurrence. Its JSON-LD
    # contains literal newlines in addresses, so strict=False is intentional.
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.get_text(), strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get('startDate'):
            continue

        event_date, time_from = parse_datetime(data.get('startDate'))
        location = data.get('location') if isinstance(data.get('location'), dict) else {}
        address = location.get('address') if isinstance(location.get('address'), dict) else {}
        title = clean_text(data.get('name'))
        venue = clean_text(location.get('name') or address.get('name'))
        city = clean_text(address.get('addressLocality'))
        canonical_url = data.get('url') if isinstance(data.get('url'), str) else url
        if not title or not event_date or not canonical_url or not venue or not city:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': canonical_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': clean_text(data.get('description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    urls = performance_urls()
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_page, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape UMS performance detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class UmsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ums_org',
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
        return get_concerts()


def main():
    return UmsOrgCrawler().run()


if __name__ == '__main__':
    main()
