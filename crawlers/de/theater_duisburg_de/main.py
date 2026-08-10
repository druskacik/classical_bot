import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-duisburg.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'spielplan/kalender/')
SOURCE = 'Theater Duisburg'
CITY = 'Duisburg'

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
    text = (
        text.replace('\xa0', ' ')
        .replace('\u202f', ' ')
        .replace('\u200b', '')
        .replace('\xad', '')
    )
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def node_text(node, selector):
    return clean_text(node.select_one(selector))


def listing_records(soup):
    records = []
    for event in soup.select('.performance.js-schedule-element'):
        link = event.select_one('a.performance__link[href]')
        start = event.select_one('meta[itemprop="startDate"][content]')
        title = node_text(event, '.performance__title [itemprop="name"]')
        venue = node_text(event, '.performance__location')
        if not link or not start or not title or not venue:
            continue

        try:
            starts_at = datetime.fromisoformat(start['content'])
        except (TypeError, ValueError):
            continue

        summary_parts = [
            node_text(event, '.performance__author'),
            node_text(event, '.performance__productioninfo'),
        ]
        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': urljoin(SOURCE_URL, link['href']),
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': 'DE',
            'description': clean_text('\n'.join(part for part in summary_parts if part)) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(soup, fallback=None):
    main = soup.select_one('main')
    if not main:
        return fallback

    parts = [
        node_text(main, '.productionhead__author'),
        node_text(main, '.productionhead__maininfo'),
    ]
    parts.extend(clean_text(node) for node in main.select('.richtext'))

    description = []
    for part in parts:
        if part and part not in description:
            description.append(part)
    return clean_text('\n\n'.join(description)) or fallback


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    calendar = get_soup(session, CALENDAR_URL)
    month_pattern = re.compile(r'/spielplan/kalender/\d{4}-\d{2}/$')
    month_urls = {
        urljoin(SOURCE_URL, link['href'])
        for link in calendar.select('a[href]')
        if month_pattern.search(link['href'].split('?', 1)[0])
    }

    records_by_key = {}
    for record in listing_records(calendar):
        records_by_key[(record['title'], record['date'], record['time_from'], record['venue'])] = record

    with ThreadPoolExecutor(max_workers=8) as executor:
        month_futures = {executor.submit(get_soup, session, url): url for url in month_urls}
        for future in as_completed(month_futures):
            url = month_futures[future]
            try:
                month_records = listing_records(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for record in month_records:
                key = (record['title'], record['date'], record['time_from'], record['venue'])
                records_by_key[key] = record

    records = list(records_by_key.values())

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, record['url']): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = detail_description(future.result(), record['description'])
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class TheaterDuisburgDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_duisburg_de',
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
    TheaterDuisburgDeCrawler().run()


if __name__ == '__main__':
    main()
