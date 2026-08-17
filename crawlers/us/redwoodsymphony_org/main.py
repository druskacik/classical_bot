import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://redwoodsymphony.org/'
SEARCH_URL = f'{SOURCE_URL}wp-json/wp/v2/search'
SOURCE = 'Redwood Symphony'
VENUE_CITY_DEFAULTS = {
    'Cañada College Main Theater': 'Redwood City',
}

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
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value):
    value = clean_text(value).split(' at ', 1)[0]
    value = re.sub(r'^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+', '', value, flags=re.I)
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    if ' AT ' in value:
        value = value.split(' AT ', 1)[1]
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def city_from_address(value):
    address = clean_text(value)
    match = re.search(r',\s*([^,]+),\s*CA(?:\s+\d{5}(?:-\d{4})?)?\s*$', address, re.I)
    return clean_text(match.group(1)) if match else ''


def discover_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            SEARCH_URL,
            params={'subtype': 'concert', 'per_page': 100, 'page': page},
            timeout=45,
        )
        response.raise_for_status()
        items = response.json()
        for item in items:
            url = item.get('url', '')
            parsed = urlparse(url)
            if (
                item.get('subtype') == 'concert'
                and parsed.scheme in {'http', 'https'}
                and parsed.netloc == 'redwoodsymphony.org'
                and parsed.path.startswith('/concert/')
            ):
                urls.append(url)

        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1

    return list(dict.fromkeys(urls))


def parse_concert_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.type-concert')
    if not article:
        return []

    title_node = soup.select_one('h1.entry-title, h1')
    title = clean_text(title_node)
    if not title:
        return []

    description_node = article.select_one('.entry-content')
    description = clean_text(description_node) or None
    records = []
    for group in article.select('.time-place-group'):
        event_date = parse_date(group.select_one('.concert-date'))
        venue = clean_text(group.select_one('.concert-venue, .concert-location'))
        city = city_from_address(group.select_one('.venue-address'))
        city = city or VENUE_CITY_DEFAULTS.get(venue, '')
        if not event_date or not venue or not city:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(group.select_one('.concert-time, .concert-date')),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_concert(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_concert_page(response.text, url)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = discover_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_concert, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Concert page request failed',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SEARCH_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class RedwoodSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='redwoodsymphony_org',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    RedwoodSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
