import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://liveartstoledo.com/toledosymphony/'
BASE_URL = 'https://liveartstoledo.com/'
LISTING_URL = 'https://liveartstoledo.com/events/toledo-symphony/'
SOURCE = 'Toledo Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

EVENT_PATH_RE = re.compile(
    r'^events/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/'
    r'toledo-symphony/[^/]+/\d+/$'
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = re.sub(r'\s+', ' ', clean_text(value))
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = re.sub(r'\s+', ' ', clean_text(value)).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_urls(session):
    urls = []
    seen = set()
    page_number = 1

    while True:
        page_url = LISTING_URL if page_number == 1 else urljoin(LISTING_URL, f'{page_number}/')
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        page_urls = []
        for link in soup.select('a[href]'):
            href = link.get('href', '').split('#', 1)[0].lstrip('/')
            if not EVENT_PATH_RE.match(href):
                continue
            url = urljoin(BASE_URL, href)
            if url not in seen:
                seen.add(url)
                page_urls.append(url)

        if not page_urls:
            break
        urls.extend(page_urls)

        next_url = urljoin(LISTING_URL, f'{page_number + 1}/')
        has_next = any(
            urljoin(BASE_URL, link.get('href', '').lstrip('/')) == next_url
            for link in soup.select('a[href]')
        )
        if not has_next:
            break
        page_number += 1

    return urls


def venue_and_city(detail):
    marker = detail.select_one('.detailRow .fa-map-marker')
    venue_link = marker.find_next('a', href=True) if marker else None
    venue = clean_text(venue_link)
    if not venue:
        return None, None

    query = parse_qs(urlparse(venue_link.get('href', '')).query)
    city = clean_text(query.get('city', [''])[0])
    return venue, city or None


def parse_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    detail = soup.select_one('.eventDetail')
    if not detail:
        return None

    title = clean_text(detail.select_one('h1.eventInfo-title'))
    event_date = parse_date(detail.select_one('.detailRow .date'))
    time_from = parse_time(detail.select_one('.detailRow .time'))
    venue, city = venue_and_city(detail)
    description = clean_text(detail.select_one('.description.detail-content')) or None

    if not all((title, event_date, venue, city)):
        log_message(
            'Skipping event with incomplete required details',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(event_date),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

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

    records = []
    for url in event_urls(session):
        try:
            record = parse_event(session, url)
        except requests.RequestException as error:
            log_message(
                'Could not retrieve event detail',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    if not records:
        log_message(
            'No Toledo Symphony events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ToledoSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='toledosymphony_com',
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
    ToledoSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
