import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filharmoniagorzowska.pl/'
REPERTOIRE_URL = urljoin(SOURCE_URL, 'repertuar')
SOURCE = 'Filharmonia Gorzowska'
CITY = 'Gorzów Wielkopolski'
# The site's archive returns January 2023 as its earliest content even when an
# older timestamp is supplied. Keeping the old timestamp makes newly restored
# archive material discoverable without hard-coding the observed first date.
ARCHIVE_TIMESTAMP = int(datetime(2010, 1, 1, tzinfo=timezone.utc).timestamp())

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_pages(session):
    base_url = f'{REPERTOIRE_URL},ts:{ARCHIVE_TIMESTAMP}'
    first_page = get_soup(session, base_url)
    yield first_page

    page_numbers = []
    for link in first_page.select('.pagination a.page-link[href]'):
        match = re.search(r'[?&]p=(\d+)', link.get('href', ''))
        if match:
            page_numbers.append(int(match.group(1)))
    last_page = max(page_numbers, default=1)
    for page_number in range(2, last_page + 1):
        yield get_soup(session, f'{base_url}?p={page_number}')


def listing_occurrences(session):
    occurrences = []
    for soup in listing_pages(session):
        for item in soup.select('.event-list.basic-list-item'):
            link = item.select_one('a.event-link[href]')
            date_node = item.select_one('.event-dates strong')
            date_text = clean_text(date_node)
            if not link or not date_text:
                continue
            try:
                event_date = date.fromisoformat(date_text).isoformat()
            except ValueError:
                continue
            date_block = clean_text(item.select_one('.event-dates'))
            time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', date_block)
            occurrences.append({
                'date': event_date,
                'time_from': time_match.group(0) if time_match else None,
                'url': urljoin(SOURCE_URL, link.get('href')),
                'listing_title': clean_text(item.select_one('.title')),
            })
    return occurrences


def resolve_city(venue):
    normalized = venue.casefold()
    if 'gorzow' in normalized or 'gorzów' in normalized or 'filharmonii gorzowskiej' in normalized:
        return CITY

    # Touring venues on this calendar normally carry their locality after a
    # comma or after Polish "w/we". Preserve that explicit locality instead of
    # applying the orchestra's home-city default.
    comma_parts = [part.strip() for part in venue.split(',') if part.strip()]
    if len(comma_parts) > 1 and re.search(r'[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]', comma_parts[-1]):
        return comma_parts[-1]
    location_match = re.search(r'\b(?:w|we)\s+([A-ZĄĆĘŁŃÓŚŹŻ][\wĄĆĘŁŃÓŚŹŻąćęłńóśźż .-]+)$', venue)
    if location_match:
        return location_match.group(1).strip()

    # The institution's repertoire is venue-based and its unnamed-locality
    # venues are in Gorzów. Explicitly named touring localities above win.
    return CITY


def detail_data(soup):
    title = clean_text(soup.select_one('.event-main .item-main-title h2'))
    venue = clean_text(soup.select_one('.event-main .venue-list .fs-18'))

    description_parts = []
    for selector, heading in (
        ('.event-program .program', 'Program'),
        ('.event-performers .list-wrapper', 'Wykonawcy'),
        ('.event-main .item-content .info-attr', None),
    ):
        text = clean_text(soup.select_one(selector))
        if text:
            description_parts.append(f'{heading}\n{text}' if heading else text)

    return {
        'title': title,
        'venue': venue,
        'city': resolve_city(venue) if venue else '',
        'description': '\n\n'.join(description_parts) or None,
    }


def make_record(occurrence, detail):
    title = detail['title'] or occurrence['listing_title']
    if not title or not detail['venue'] or not detail['city']:
        return None
    return {
        'title': title,
        'date': occurrence['date'],
        'url': occurrence['url'],
        'time_from': occurrence['time_from'],
        'venue': detail['venue'],
        'city': detail['city'],
        'country_code': 'PL',
        'description': detail['description'],
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    occurrences = listing_occurrences(session)
    details = {}

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_soup, session, url): url
            for url in {occurrence['url'] for occurrence in occurrences}
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = detail_data(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = [
        make_record(occurrence, details[occurrence['url']])
        for occurrence in occurrences
        if occurrence['url'] in details
    ]
    return sorted(
        (record for record in records if record),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FilharmoniaGorzowskaPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmoniagorzowska_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
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
    FilharmoniaGorzowskaPlCrawler().run()


if __name__ == '__main__':
    main()
