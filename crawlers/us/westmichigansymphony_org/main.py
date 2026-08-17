import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://westmichigansymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}bevent-sitemap.xml'
SOURCE = 'West Michigan Symphony'
CITY = 'Muskegon'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+)?'
    r'(\d{1,2}\.\d{1,2}\.\d{2,4})'
    r'(?:\s*\|\s*(\d{1,2}(?::\d{2})?\s*[ap]m))?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return '', None
    raw_date, raw_time = match.groups()
    event_date = ''
    for pattern in ('%m.%d.%y', '%m.%d.%Y'):
        try:
            event_date = datetime.strptime(raw_date, pattern).date().isoformat()
            break
        except ValueError:
            pass

    event_time = None
    if raw_time:
        normalized = re.sub(r'\s+', ' ', raw_time).strip().upper()
        for pattern in ('%I:%M %p', '%I %p'):
            try:
                event_time = datetime.strptime(normalized, pattern).strftime('%H:%M')
                break
            except ValueError:
                pass
    return event_date, event_time


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return sorted({
        clean_text(node)
        for node in soup.select('loc')
        if clean_text(node).startswith(f'{SOURCE_URL}events/')
    })


def parse_event(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Event page request failed',
            event='crawler_event_request_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    event_date, event_time = parse_date_time(soup.select_one('.event-date'))
    venue = clean_text(soup.select_one('.event-venue'))
    if not title or not event_date or not venue:
        return None

    description = clean_text(soup.select_one('.tab-content')) or None
    return {
        'title': title,
        'date': event_date,
        'url': response.url,
        'time_from': event_time,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_event, url): url for url in urls}
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class WestMichiganSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='westmichigansymphony_org',
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
    WestMichiganSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
