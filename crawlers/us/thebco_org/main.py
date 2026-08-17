import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://thebco.org/'
EVENT_SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-event-1.xml'
SOURCE = 'Baltimore Chamber Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s+at\s+'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)

VENUE_LOCATIONS = {
    'kraushaar auditorium': ('Kraushaar Auditorium', 'Towson'),
    'leclerc hall': ('LeClerc Hall', 'Baltimore'),
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        event_time = datetime.strptime(
            re.sub(r'\s+', '', match.group(2)).upper(),
            '%I:%M%p' if ':' in match.group(2) else '%I%p',
        ).strftime('%H:%M')
        return event_date, event_time
    except ValueError:
        return None, None


def parse_venue(value):
    normalized = clean_text(value).lower().replace('’', "'")
    for needle, location in VENUE_LOCATIONS.items():
        if needle in normalized:
            return location
    return None, None


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    headings = [
        clean_text(node.get_text(' ', strip=True))
        for node in soup.select('h1.elementor-heading-title')
    ]
    if len(headings) < 3:
        return None

    title = headings[0]
    event_date, time_from = parse_date_time(headings[1])
    venue, city = parse_venue(headings[2])
    first_heading = soup.select_one('h1.elementor-heading-title')
    content = first_heading.find_parent('div', class_='elementor') if first_heading else None
    description = clean_text(content.get_text('\n', strip=True)) if content else None

    if not title or not event_date or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch(session, url, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            log_message(
                'BCO request failed',
                event='crawler_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            if attempt < attempts:
                time.sleep(5 * attempt)
    return None


def scrape_concerts(session=None, request_delay=2):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = fetch(session, EVENT_SITEMAP_URL)
    if response is None:
        return []

    sitemap = BeautifulSoup(response.content, 'xml')
    urls = [
        clean_text(node.get_text())
        for node in sitemap.find_all('loc')
        if '/event/' in node.get_text()
    ]

    records = []
    for index, url in enumerate(urls):
        if index and request_delay:
            time.sleep(request_delay)
        detail = fetch(session, url)
        if detail is None:
            continue
        record = parse_event_page(detail.text, url)
        if record:
            records.append(record)
        else:
            log_message(
                'BCO event page could not be parsed',
                event='crawler_parse_failed',
                level='warning',
                url=url,
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TheBcoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thebco_org',
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
    TheBcoOrgCrawler().run()


if __name__ == '__main__':
    main()
