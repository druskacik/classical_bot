import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera-nancy.fr/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendrier')
SOURCE = 'Opéra national de Nancy-Lorraine'
CITY = 'Nancy'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_urls(session):
    # Removing the calendar's default "now" boundary exposes its published
    # archive (currently back to the 2020/21 season).
    params = {
        'category': 'All',
        'location': 'All',
        'ssks_datepicker_start_date_min': '1900-01-01',
        'ssks_datepicker_end_date_min': '1900-01-01',
    }
    urls = set()
    for page in range(100):
        params['page'] = page
        soup = get_soup(session, CALENDAR_URL, params=params)
        articles = soup.select('.views-results article.enlarge-link')
        for article in articles:
            link = article.select_one('h2 a[href]')
            if link:
                urls.add(urljoin(SOURCE_URL, link.get('href')))

        next_links = soup.select('.pager a[href]')
        if not any(f'page={page + 1}' in (link.get('href') or '') for link in next_links):
            break
    else:
        log_message(
            'Calendar pagination limit reached',
            event='crawler_pagination_limit',
            level='warning',
            url=CALENDAR_URL,
        )
    return sorted(urls)


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') in ('Event', 'EventSeries'):
                return item
    return None


def description_from_page(soup, schema):
    editorial = soup.select_one('.field--name-field-editorial')
    description = clean_text(editorial) if editorial else ''
    return description or clean_text(schema.get('description')) or None


def occurrence_record(series, occurrence, page_url, description):
    start = occurrence.get('startDate')
    try:
        parsed_start = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None

    location = occurrence.get('location') or series.get('location') or {}
    if not isinstance(location, dict):
        return None
    address = location.get('address') or {}
    if not isinstance(address, dict):
        return None
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country = clean_text(address.get('addressCountry')).upper()
    if country not in ('FR', 'FRA', 'FRANCE'):
        return None
    country_code = 'FR'
    # Imported archive entries sometimes omit the locality while retaining the
    # institution's unambiguous home venue. Explicit touring venues are never
    # assigned this default.
    if not city and venue.casefold() == SOURCE.casefold():
        city = CITY

    title = clean_text(series.get('name') or occurrence.get('name'))
    occurrence_url = occurrence.get('url') or page_url
    url = urljoin(SOURCE_URL, occurrence_url)
    if not all((title, venue, city, url)):
        return None
    return {
        'title': title,
        'date': parsed_start.date().isoformat(),
        'url': url,
        'time_from': parsed_start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(session, url):
    soup = get_soup(session, url)
    schema = event_schema(soup)
    if not schema:
        return []
    occurrences = schema.get('subEvent') or [schema]
    if isinstance(occurrences, dict):
        occurrences = [occurrences]
    description = description_from_page(soup, schema)
    records = []
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        record = occurrence_record(schema, occurrence, url, description)
        if record:
            records.append(record)
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = calendar_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class OperaNancyFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_nancy_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
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
    OperaNancyFrCrawler().run()


if __name__ == '__main__':
    main()
