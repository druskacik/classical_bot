import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theaterdo.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalender/')
SOURCE = 'Theater Dortmund'
CITY = 'Dortmund'
TIMEZONE = ZoneInfo('Europe/Berlin')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_listing(soup):
    records = []
    for event in soup.select('.solr-results__list .event[data-start]'):
        title_node = event.select_one('.event__info h2')
        link = event.select_one('.event__info a[href]')
        detail_spans = event.select('.event__details span')
        venue = clean_text(detail_spans[0]) if detail_spans else ''
        if not title_node or not link or not venue or venue.lower().startswith('mobil buchbar'):
            continue

        try:
            starts_at = datetime.fromtimestamp(int(event['data-start']), tz=TIMEZONE)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        # A few undated productions are emitted with Unix epoch zero. They are
        # catalogue placeholders, not real performances.
        if starts_at.year < 2000:
            continue

        summary_parts = [clean_text(event.select_one('.event__details p'))]
        summary_parts.extend(clean_text(node) for node in event.select('.event__textfield'))
        records.append({
            'title': clean_text(title_node),
            'date': starts_at.date().isoformat(),
            'url': urljoin(SOURCE_URL, link['href']),
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': 'DE',
            'description': clean_text('\n\n'.join(part for part in summary_parts if part)) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(soup):
    production = soup.select_one('main .tx-ra-eventim')
    if not production:
        return None

    parts = []
    subtitle = production.select_one('.tx-ra-eventim-header__subtitle')
    if subtitle:
        parts.append(clean_text(subtitle))

    for section in production.select('.section--white .section__content'):
        classes = section.get('class', [])
        if 'section__content--image' in classes:
            continue
        text = clean_text(section)
        if text and text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_page = get_soup(session, CALENDAR_URL)

    pages = {1: first_page}
    # Solr's pager only exposes a short window, not the true last page. Fetch in
    # bounded batches until the first batch containing an empty page.
    next_page = 2
    while next_page <= 100:
        numbers = range(next_page, next_page + 8)
        batch = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(get_soup, session, f'{CALENDAR_URL}?tx_solr%5Bpage%5D={number}'): number
                for number in numbers
            }
            for future in as_completed(futures):
                number = futures[future]
                try:
                    batch[number] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape calendar page',
                        event='crawler_page_failed',
                        level='warning',
                        url=f'{CALENDAR_URL}?tx_solr%5Bpage%5D={number}',
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        pages.update({number: soup for number, soup in batch.items() if parse_listing(soup)})
        if any(number in batch and not parse_listing(batch[number]) for number in numbers):
            break
        next_page += 8

    records_by_key = {}
    for number in sorted(pages):
        for record in parse_listing(pages[number]):
            key = (record['title'], record['date'], record['time_from'], record['venue'])
            records_by_key[key] = record
    records = list(records_by_key.values())

    descriptions = {}
    detail_urls = {record['url'] for record in records}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = detail_description(future.result())
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
        record['description'] = descriptions.get(record['url']) or record['description']
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']))


class TheaterdoDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theaterdo_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        return get_concerts()


def main():
    TheaterdoDeCrawler().run()


if __name__ == '__main__':
    main()
