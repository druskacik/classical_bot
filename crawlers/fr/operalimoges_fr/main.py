import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operalimoges.fr/'
SEARCH_URL = urljoin(SOURCE_URL, 'search')
SOURCE = 'Opéra de Limoges'
CITY = 'Limoges'
LOCAL_TIMEZONE = ZoneInfo('Europe/Paris')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
SEARCH_PARAMS = {
    'date[min]': '',
    'date[max]': '',
    'duration': 'All',
    'field_start_price': 'All',
    'field_location': 'All',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def detail_urls(session):
    soup = get_soup(session, SEARCH_URL, params=SEARCH_PARAMS)
    urls = set()
    for row in soup.select('.views-row'):
        link = row.select_one('a[href]')
        if not link:
            continue
        url = urljoin(SOURCE_URL, link.get('href')).split('#', 1)[0]
        if url.startswith(SOURCE_URL) and url != SEARCH_URL:
            urls.add(url)
    return sorted(urls)


def parse_start(value):
    try:
        start = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    if start.tzinfo is not None:
        start = start.astimezone(LOCAL_TIMEZONE)
    return start.date().isoformat(), start.strftime('%H:%M')


def parse_detail(session, url):
    soup = get_soup(session, url)
    title_node = soup.select_one('h1')
    location_node = soup.select_one(
        '.node--view-mode-sidebar-information .field--name-field-location .field__item'
    )
    if not location_node:
        location_node = soup.select_one(
            '.field-info-event > .field--name-field-location > .field__item'
        )
    title = clean_text(title_node)
    venue = clean_text(location_node)
    # "Hors les murs" does not identify the actual venue or even establish
    # that the occurrence is in Limoges, so those entries cannot form valid
    # records until the site publishes a concrete location.
    if not title or not venue or venue.casefold() == 'hors les murs':
        return []

    description_node = soup.select_one(
        '.node--view-mode-full .field--name-field-summary .field__item'
    )
    description = clean_text(description_node) or None
    records = []
    seen = set()
    for node in soup.select(
        '.node--view-mode-sidebar-information '
        '.field--name-field-date time[datetime]'
    ):
        occurrence = parse_start(node.get('datetime'))
        if not occurrence or occurrence in seen:
            continue
        seen.add(occurrence)
        event_date, time_from = occurrence
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class OperaLimogesFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operalimoges_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
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
        urls = detail_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(parse_detail, session, url): url for url in urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Opéra de Limoges event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OperaLimogesFrCrawler().run()


if __name__ == '__main__':
    main()
