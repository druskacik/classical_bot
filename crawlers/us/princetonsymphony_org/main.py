import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://princetonsymphony.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Princeton Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Matthews Theater': 'Princeton',
    'Matthews Theatre': 'Princeton',
    'New Brunswick Performing Arts Center': 'New Brunswick',
    'Performance Pavilion - Morven Museum & Garden': 'Princeton',
    'Richardson Auditorium': 'Princeton',
    'Trinity Church': 'Princeton',
    'Wolfensohn Hall - Institute for Advanced Study': 'Princeton',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_occurrence(time_node):
    value = clean_text(time_node)
    match = re.search(
        r'([A-Za-z]+),?\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s*-\s*'
        r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    _, month, day, year, event_time = match.groups()
    try:
        date = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
        time_from = datetime.strptime(event_time.replace(' ', '').upper(), '%I:%M%p').strftime('%H:%M')
    except ValueError:
        try:
            time_from = datetime.strptime(event_time.replace(' ', '').upper(), '%I%p').strftime('%H:%M')
        except ValueError:
            return None
    return date, time_from


def listing_urls(session):
    response = session.get(CALENDAR_URL, params={'type_1': 'performance'}, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = {
        urljoin(CALENDAR_URL, link['href'])
        for link in soup.select('.node--type-performance a[href*="/performances/"]')
    }
    return sorted(urls)


def parse_performance(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one('.node--type-performance.node--view-mode-full')
    if not node:
        return []

    title = clean_text(node.select_one('h1'))
    venue = clean_text(node.select_one('.field--name-field-venue'))
    city = VENUE_CITIES.get(venue)
    if not title or not venue or not city:
        return []

    description_parts = []
    for selector in ('.field--name-body', '.field--name-field-program'):
        field = node.select_one(selector)
        text = clean_text(field)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    date_field = node.select_one('.performance-title-container .field--name-field-date-start')
    for time_node in date_field.select('time') if date_field else []:
        occurrence = parse_occurrence(time_node)
        if not occurrence:
            continue
        date, time_from = occurrence
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            parsed = parse_performance(response.text, url)
            if not parsed:
                log_message(
                    'Performance page had no valid occurrences',
                    event='crawler_invalid_performance',
                    level='warning',
                    url=url,
                )
            records.extend(parsed)
        except requests.RequestException as error:
            log_message(
                'Could not fetch performance page',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No performances found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class PrincetonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='princetonsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PrincetonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
