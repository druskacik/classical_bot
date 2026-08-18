import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://dubuquesymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-and-events')
EVENT_DATA_URL = urljoin(SOURCE_URL, 'concerts-and-events/script?v=2.3')
SOURCE = 'Dubuque Symphony Orchestra'
CITY = 'Dubuque'

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
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = re.sub(r'^[A-Za-z]+\s+', '', clean_text(value))
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(value.upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_urls(script):
    urls = set()
    pattern = re.compile(
        r"date_link:\s*'(?P<timestamp>\d+)'[\s\S]*?"
        r"url_title:\s*'(?P<slug>[^']+)'",
    )
    for match in pattern.finditer(script):
        urls.add(urljoin(
            LISTING_URL + '/',
            f"{match.group('slug')}/{match.group('timestamp')}",
        ))
    return sorted(urls)


def parse_event_page(soup, url):
    title_node = soup.select_one('h1')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')

    timestamp = urlparse(url).path.rstrip('/').rsplit('/', 1)[-1]
    try:
        url_date = datetime.fromtimestamp(
            int(timestamp), tz=ZoneInfo('America/Chicago')
        ).date().isoformat()
    except (ValueError, OverflowError, OSError):
        url_date = ''

    matching_ticket = None
    for ticket in soup.select('.find-tickets .ticket'):
        date_node = ticket.select_one('.date')
        if date_node and parse_date(date_node.get_text(' ', strip=True)) == url_date:
            matching_ticket = ticket
            break

    if not matching_ticket:
        return None

    date_node = matching_ticket.select_one('.date')
    time_node = matching_ticket.select_one('.time')
    venue_node = matching_ticket.select_one('.location')
    event_date = parse_date(date_node.get_text(' ', strip=True) if date_node else '')
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')

    description_parts = []
    for selector in ('.about-info', '.program-copy'):
        node = soup.select_one(selector)
        text = clean_text(node.get_text('\n', strip=True) if node else '')
        if text:
            description_parts.append(text)

    if not title or not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_node.get_text(' ', strip=True)) if time_node else None,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    data_response = session.get(EVENT_DATA_URL, timeout=45)
    data_response.raise_for_status()
    urls = event_urls(data_response.text)

    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_event_page(BeautifulSoup(response.text, 'html.parser'), url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete details',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Event request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class DubuqueSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dubuquesymphony_org',
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
    DubuqueSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
