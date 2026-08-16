import json
import re
from datetime import date
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orcma.org/'
SITEMAP_URL = urljoin(
    SOURCE_URL,
    'dynamic-concerts-and-events_p_0aee4ec5_0c93_4300_9536_b3f06ccf8940_0_5000-sitemap.xml',
)
SOURCE = 'Oak Ridge Civic Music Association'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = re.fullmatch(r'(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?', value or '')
    if not match:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def normalized_path(value):
    return unquote(urlparse(value or '').path).rstrip('/')


def records_from_warmup(payload, expected_url=None):
    try:
        data = json.loads(payload)
        items = (
            data['appsWarmupData']['dataBinding']['dataStore']
            ['recordsByCollectionId']['ConcertsAndEvents']
        ).values()
    except (KeyError, TypeError, json.JSONDecodeError):
        return []

    records = []
    for item in items:
        path = item.get('link-concerts-and-events-eventName')
        if expected_url and normalized_path(path) != normalized_path(expected_url):
            continue
        if 'Concert' not in (item.get('category') or []):
            continue

        title = clean_text(item.get('eventName'))
        event_date = item.get('date')
        venue = clean_text(item.get('location'))
        address = item.get('address') or {}
        if isinstance(address, dict):
            city = clean_text(address.get('city'))
            country_code = clean_text(address.get('country')).upper()
        else:
            address_text = clean_text(address)
            city_match = re.search(r',\s*([^,]+?),?\s+[A-Z]{2}(?:\s+\d{5})?\b', address_text)
            city = clean_text(city_match.group(1)) if city_match else ''
            country_code = 'US' if city else ''
        if not city and 'oak ridge' in venue.lower():
            city = 'Oak Ridge'
        if not country_code and city == 'Oak Ridge':
            country_code = 'US'
        if not (
            title and valid_date(event_date) and venue and city and
            re.fullmatch(r'[A-Z]{2}', country_code) and path
        ):
            continue

        descriptions = []
        for field in ('longDescription', 'shortDescription'):
            value = clean_text(item.get(field))
            if value and value not in descriptions:
                descriptions.append(value)

        records.append({
            'title': title,
            'date': event_date,
            'url': urljoin(SOURCE_URL, path),
            'time_from': parse_time(item.get('time')),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': '\n\n'.join(descriptions) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def scrape_concerts(session=None):
    if session is None:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )
        session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = [
        node.get_text(strip=True) for node in soup.select('url > loc')
        if '/concerts-and-events/' in node.get_text()
    ]

    records = []
    for url in urls:
        try:
            detail = session.get(url, timeout=45)
            detail.raise_for_status()
            detail_soup = BeautifulSoup(detail.text, 'html.parser')
            warmup = detail_soup.select_one('#wix-warmup-data')
            records.extend(records_from_warmup(warmup.get_text() if warmup else '', url))
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    records.sort(key=lambda item: (item['date'], item['time_from'] or '', item['title']))

    if not records:
        log_message(
            'No concert records found in Wix warmup data',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return records


class OrcmaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orcma_org',
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
    OrcmaOrgCrawler().run()


if __name__ == '__main__':
    main()
