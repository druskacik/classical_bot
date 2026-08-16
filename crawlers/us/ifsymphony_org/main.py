import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ifsymphony.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
SOURCE = 'Idaho Falls Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The first-party calendar's named venues are all in Idaho Falls. Keeping an
# explicit map avoids assigning the orchestra's home city to a future tour.
VENUE_CITIES = {
    'Frontier Center for the Performing Arts': 'Idaho Falls',
    'Colonial Theater': 'Idaho Falls',
    'The Downtown Event Center (DEC)': 'Idaho Falls',
    'Freeman Park Amphitheater': 'Idaho Falls',
    'ARTitorium on Broadway': 'Idaho Falls',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%m/%d/%Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(value.replace(' ', ''), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def calendar_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for link in soup.select('main a[href]'):
        url = urljoin(CALENDAR_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc == urlparse(SOURCE_URL).netloc and parsed.path.startswith('/calendar/'):
            if parsed.path.rstrip('/') != '/calendar':
                urls.add(url.split('#', 1)[0])
    return sorted(urls)


def detail_record(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    info = soup.select_one('main .calendar-item .event-info')
    if not info:
        return None

    title_node = info.select_one('h1')
    date_node = info.select_one('.event-meta .date-icon')
    venue_node = info.select_one('.event-meta .location-icon')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    event_date = parse_date(date_node.get_text(' ', strip=True) if date_node else '')
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
    city = VENUE_CITIES.get(venue, '')
    if not title or not event_date or not venue or not city:
        return None

    time_node = info.select_one('.event-meta .time-icon')
    time_from = parse_time(time_node.get_text(' ', strip=True)) if time_node else None

    body = BeautifulSoup(str(info), 'html.parser')
    for node in body.select('h1, .categories, .event-meta, script, style, a.btn'):
        node.decompose()
    description = body.get_text('\n', strip=True)
    description = re.sub(r'[ \t]+', ' ', description)
    description = re.sub(r' *\n *', '\n', description)
    description = re.sub(r'\n{3,}', '\n\n', description).strip() or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()

    records = []
    urls = calendar_urls(response.text)
    for url in urls:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            record = detail_record(detail_response.text, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping calendar item without a usable date, venue, or city',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Failed to fetch calendar item',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No usable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class IfsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ifsymphony_org',
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
    IfsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
