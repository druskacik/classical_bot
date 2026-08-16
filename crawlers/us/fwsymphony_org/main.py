import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fwsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-tickets/upcoming-concerts')
SOURCE = 'Fort Worth Symphony Orchestra'
CITY = 'Fort Worth'

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
    return re.sub(r'\s+', ' ', value.get_text(' ', strip=True).replace('\xa0', ' ')).strip()


def section_text(soup, heading):
    node = soup.find(
        ['h2', 'h3'],
        string=lambda value: value and value.strip().lower() == heading.lower(),
    )
    if not node:
        return ''
    container = node.parent
    parts = []
    for child in container.find_all(['p', 'li'], recursive=True):
        text = clean_text(child)
        if text and text not in parts:
            parts.append(text)
    return '\n'.join(parts)


def parse_occurrence(value):
    value = re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()
    for pattern in ('%a, %b %d, %Y, %I:%M %p', '%A, %B %d, %Y, %I:%M %p'):
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_detail(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    title_node = soup.select_one('main h1')
    dates = soup.select('.pdp-overview-date-list-item h3')
    location_heading = soup.find(
        ['h2', 'h3'], string=lambda value: value and value.strip().lower() == 'location'
    )
    venue_node = location_heading.find_next('p') if location_heading else None

    title = clean_text(title_node)
    venue = clean_text(venue_node)
    if not title or not venue or not dates:
        log_message(
            'Concert detail is missing required fields',
            event='crawler_detail_incomplete',
            level='warning',
            url=url,
        )
        return []

    description_parts = []
    for heading in ('Overview', 'Works', 'Featured Artists'):
        text = section_text(soup, heading)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for date_node in dates:
        occurrence = parse_occurrence(clean_text(date_node))
        if not occurrence:
            continue
        event_date, time_from = occurrence
        records.append({
            'title': title,
            'date': event_date,
            'url': response.url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    urls = []
    for link in soup.select('.pod-listing .pod-item h3 a[href]'):
        url = urljoin(response.url, link['href'])
        if url not in urls:
            urls.append(url)

    if not urls:
        log_message(
            'No concert links found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
        return []

    records = []
    for url in urls:
        records.extend(parse_detail(session, url))
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class FwSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fwsymphony_org',
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
    FwSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
