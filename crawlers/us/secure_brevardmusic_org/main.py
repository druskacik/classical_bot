import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brevardmusic.org/'
EVENTS_URL = f'{SOURCE_URL}events/'
SOURCE = 'Brevard Music Center'
CITY = 'Brevard'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    value = clean_text(value)
    for pattern in ('%A, %B %d, %Y, %I:%M %p', '%A, %B %d, %Y'):
        try:
            parsed = datetime.strptime(value, pattern)
            time_from = parsed.strftime('%H:%M') if '%I' in pattern else None
            return parsed.date().isoformat(), time_from
        except ValueError:
            pass
    return None, None


def description_from_detail(html):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.event')
    if not article:
        return None

    parts = []
    for selector in ('.program', '.content .body'):
        node = article.select_one(selector)
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for article in soup.select('article.event'):
        title_link = article.select_one('.header h1 a[href]')
        date_node = article.select_one('.header .date')
        venue_node = article.select_one('.header .location')
        if not title_link or not date_node or not venue_node:
            continue

        title = clean_text(title_link)
        event_date, time_from = parse_datetime(date_node)
        venue = clean_text(venue_node)
        url = title_link.get('href', '').strip()
        if not title or not event_date or not venue or not url.startswith(('http://', 'https://')):
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_description(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return description_from_detail(response.text)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(
        EVENTS_URL,
        params={'start-date': '1900-01-01', 'end-date': '2100-12-31'},
        timeout=60,
    )
    response.raise_for_status()
    records = parse_listing(response.text)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_description, record['url']): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No event records found',
            event='crawler_empty_listing',
            level='warning',
            url=response.url,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SecureBrevardmusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='secure_brevardmusic_org',
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
    SecureBrevardmusicOrgCrawler().run()


if __name__ == '__main__':
    main()
