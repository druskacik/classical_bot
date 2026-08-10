import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-pforzheim.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'spielplan.html')
SOURCE = 'Theater Pforzheim'

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
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def schedule_page_urls(soup):
    urls = {SCHEDULE_URL}
    pattern = re.compile(r'/spielplan/events-page-(\d+)\.html$')
    page_numbers = []
    for link in soup.select('a[href]'):
        href = link.get('href', '').split('?', 1)[0]
        match = pattern.search(href)
        if match:
            page_numbers.append(int(match.group(1)))
    if page_numbers:
        urls.update(
            urljoin(SOURCE_URL, f'spielplan/events-page-{page}.html')
            for page in range(2, max(page_numbers) + 1)
        )
    return urls


def parse_listing_item(item):
    link = item.select_one('.h3 a[href*="/event/eventDetail/"]')
    venue_link = item.select_one('.list-group-item a[href]')
    if not link or not venue_link:
        return None

    url = urljoin(SOURCE_URL, link.get('href', ''))
    match = re.search(r'/eventDetail/(\d{4}-\d{2}-\d{2})_(\d{4})/', url)
    title = clean_text(link)
    venue = clean_text(venue_link)
    if not match or not title or not venue:
        return None

    try:
        date = datetime.strptime(match.group(1), '%Y-%m-%d').date().isoformat()
        time_from = datetime.strptime(match.group(2), '%H%M').strftime('%H:%M')
    except ValueError:
        return None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': None,
        'country_code': 'DE',
        'description': clean_text(item.select_one('.teaser-list')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_data(session, url):
    soup = get_soup(session, url)
    event = soup.select_one('.events-single')
    if not event:
        return None

    venue_link = event.select_one('.infobox dd.icon-text a[href*="location"]')
    address = event.select_one('.infobox address')
    venue = clean_text(venue_link)
    address_text = clean_text(address)
    city_match = re.search(r'\b\d{5}\s+([^\n,]+)', address_text)
    if not venue or not city_match:
        return None

    city = city_match.group(1).strip()
    description_parts = [
        clean_text(event.select_one('h2')),
        clean_text(event.select_one('.events-main')),
    ]
    description = '\n\n'.join(part for part in description_parts if part)
    return venue, city, description or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_page = get_soup(session, SCHEDULE_URL)

    page_urls = schedule_page_urls(first_page)
    pages = {SCHEDULE_URL: first_page}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, session, url): url
            for url in page_urls
            if url != SCHEDULE_URL
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                pages[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape schedule page',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records_by_key = {}
    for soup in pages.values():
        for item in soup.select('.tx-events2 .layout-list .item'):
            record = parse_listing_item(item)
            if record:
                key = (record['url'], record['date'], record['time_from'], record['venue'])
                records_by_key[key] = record

    records = list(records_by_key.values())
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(detail_data, session, record['url']): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                detail = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if detail:
                record['venue'], record['city'], description = detail
                if description:
                    record['description'] = description

    return sorted(
        (record for record in records if record['city'] and record['venue']),
        key=lambda record: (record['date'], record['time_from'], record['title'], record['venue']),
    )


class TheaterPforzheimDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_pforzheim_de',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterPforzheimDeCrawler().run()


if __name__ == '__main__':
    main()
