import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera.ee/et/'
API_URL = urljoin(SOURCE_URL, 'api/shows/')
SOURCE = 'Rahvusooper Estonia'
DEFAULT_VENUE = 'Rahvusooper Estonia'
DEFAULT_CITY = 'Tallinn'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'et-EE,et;q=0.9,en;q=0.7',
}

# The API uses these venue names for performances outside the opera house.
# Both are established venues in Tallinn. Unknown explicit locations are
# skipped rather than being assigned an unjustified city.
VENUE_CITIES = {
    'Estonia kontserdisaal': 'Tallinn',
    'Tallinna Jaani kirik': 'Tallinn',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def event_location(item):
    location = item.get('location')
    if not location:
        return DEFAULT_VENUE, DEFAULT_CITY

    venue = clean_text(location.get('name'))
    city = VENUE_CITIES.get(venue)
    if not venue or not city:
        log_message(
            'Skipping performance with unknown explicit venue',
            event='crawler_item_skipped',
            level='warning',
            url=urljoin(SOURCE_URL, item.get('url') or ''),
            venue=venue,
        )
        return None
    return venue, city


def api_record(item):
    staging = item.get('staging') or {}
    title = clean_text(staging.get('name'))
    relative_url = item.get('url') or ''
    location = event_location(item)
    try:
        start = datetime.fromisoformat(item.get('time') or '')
    except (TypeError, ValueError):
        return None

    url = urljoin(SOURCE_URL, relative_url)
    if not title or not relative_url or not location:
        return None

    venue, city = location
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'EE',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
        '_detail_url': urljoin(SOURCE_URL, staging.get('url') or relative_url),
    }


def detail_description(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('main .content')
    return clean_text(content) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    records = []
    page_url = API_URL
    params = {'size': 500}
    while page_url:
        payload = get_json(session, page_url, params=params)
        params = None
        for item in payload.get('results', []):
            record = api_record(item)
            if record:
                records.append(record)
        page_url = payload.get('next')

    detail_urls = {record['_detail_url'] for record in records}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, url): url
            for url in detail_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        record['description'] = descriptions.get(record.pop('_detail_url'))

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class OperaEeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_ee',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='EE',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaEeCrawler().run()


if __name__ == '__main__':
    main()
