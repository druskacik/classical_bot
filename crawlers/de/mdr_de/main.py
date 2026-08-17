import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mdr.de/konzerte/konzertkalender/'
SOURCE = 'MDR KLASSIK'
CALENDAR_URL = SOURCE_URL
FIRST_ARCHIVE_YEAR = 2016

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'Januar': 1, 'Februar': 2, 'März': 3, 'April': 4,
    'Mai': 5, 'Juni': 6, 'Juli': 7, 'August': 8,
    'September': 9, 'Oktober': 10, 'November': 11, 'Dezember': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\xad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_cursors():
    today = date.today()
    for year in range(FIRST_ARCHIVE_YEAR, today.year + 2):
        for month in range(1, 13):
            # The calendar returns the next 20 concerts from the selected date.
            # Two cursors per month prevent dense festival periods being clipped.
            yield date(year, month, 1).isoformat()
            yield date(year, month, 15).isoformat()


def parse_listing(soup):
    records = []
    for item in soup.select('li.program'):
        link = item.select_one('a[title="Veranstaltungsdetails"][href]')
        title = clean_text(link)
        day = clean_text(item.select_one('.brandeddate .date')).rstrip('.')
        month_name = clean_text(item.select_one('.brandeddate .month'))
        year = clean_text(item.select_one('.brandeddate .year'))
        location = clean_text(item.select_one('.details .subtitle'))
        if not link or not title or not day or month_name not in MONTHS or not year or ',' not in location:
            continue

        try:
            event_date = date(int(year), MONTHS[month_name], int(day)).isoformat()
        except ValueError:
            continue

        city, venue = (part.strip() for part in location.split(',', 1))
        if not city or not venue:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': urljoin(SOURCE_URL, link['href']),
            'time_from': clean_text(item.select_one('.performedby .time')) or None,
            'venue': venue,
            'city': city,
            'description': None,
        })
    return records


def detail_description(soup):
    article = soup.select_one('article.articlepage')
    if not article:
        return None
    content = article.select_one('.contentbox.boc-ticketbox')
    return clean_text(content) or clean_text(article) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records_by_key = {}

    def fetch_listing(cursor):
        soup = get_soup(session, CALENDAR_URL, {'brand': 'all', 'date': cursor})
        return parse_listing(soup)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_listing, cursor): cursor for cursor in calendar_cursors()}
        for future in as_completed(futures):
            cursor = futures[future]
            try:
                records = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert calendar',
                    event='crawler_page_failed', level='warning',
                    url=f'{CALENDAR_URL}?brand=all&date={cursor}',
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            for record in records:
                key = (record['url'], record['date'], record['time_from'], record['venue'])
                records_by_key[key] = record

    records = list(records_by_key.values())
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, record['url']): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = detail_description(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed', level='warning', url=record['url'],
                    error_type=type(error).__name__, error_message=str(error),
                )

    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class MdrDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mdr_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    MdrDeCrawler().run()


if __name__ == '__main__':
    main()
