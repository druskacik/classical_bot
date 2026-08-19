import json
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.proarte.org/'
SOURCE = 'Pro Arte Chamber Orchestra'
SITEMAP_URL = (
    'https://www.proarte.org/'
    'dynamic-concerts_p_1c149eef_07ce_4ede_ad37_628d0667b3cf_0_5000-sitemap.xml'
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_html(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def concert_item(warmup_data, page_url):
    page_path = urlparse(page_url).path.rstrip('/')
    candidates = []
    for item in walk_dicts(warmup_data):
        item_path = str(item.get('link-items-title') or '').rstrip('/')
        if item_path == page_path and item.get('date') and item.get('title'):
            candidates.append(item)
    return candidates[0] if candidates else None


def parse_time(value):
    text = clean_html(value).upper().replace(' ', '')
    if not text:
        return None
    for pattern in ('%I:%M%p', '%I%p', '%H:%M'):
        try:
            return datetime.strptime(text, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def record_from_item(item, page_url):
    title = clean_html(item.get('title'))
    venue = clean_html(item.get('venue'))
    address = item.get('address') if isinstance(item.get('address'), dict) else {}
    city = clean_html(address.get('city'))
    country_code = clean_html(address.get('country')).upper()

    try:
        date = datetime.strptime(str(item.get('date')), '%Y-%m-%d').date().isoformat()
    except (TypeError, ValueError):
        return None

    if not title or not venue or not city or country_code != 'US':
        return None

    description_parts = []
    for field in ('itemPageText', 'musicalProgram', 'subtitle'):
        text = clean_html(item.get(field))
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': date,
        'url': page_url,
        'time_from': parse_time(item.get('time')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    return [
        node.get_text(strip=True)
        for node in soup.select('url > loc')
        if '/concerts/' in node.get_text()
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for page_url in sitemap_urls(session):
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            data_node = soup.select_one('script#wix-warmup-data[type="application/json"]')
            if data_node is None:
                continue
            item = concert_item(json.loads(data_node.get_text()), page_url)
            record = record_from_item(item, page_url) if item else None
            if record:
                records.append(record)
        except (requests.RequestException, json.JSONDecodeError) as error:
            log_message(
                'Failed to parse Pro Arte concert page',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No Pro Arte concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return records


class ProarteOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='proarte_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ProarteOrgCrawler().run()


if __name__ == '__main__':
    main()
