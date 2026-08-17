import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://philorch.ensembleartsphilly.org/'
EVENTS_URL = 'https://ds1hlj0wwv74x.cloudfront.net/Prod/events/poa/live'
SOURCE = 'The Philadelphia Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
    'Upgrade-Insecure-Requests': '1',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def fetch_description(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Unable to fetch concert detail',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    parts = []
    for node in soup.select('.content-blocks .text-content, .content-blocks .program-panel'):
        text = clean_text(node.get_text(' ', strip=True))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def event_records(event, description=None):
    title = clean_text(event.get('Title'))
    url = clean_text(event.get('Link'))
    venue_data = event.get('Venue') or {}
    venue = clean_text(venue_data.get('Title'))
    city = clean_text(venue_data.get('AddressLocality'))
    performances = event.get('Performances') or []
    if not title or not url or not venue or not city:
        return []

    records = []
    for performance in performances:
        value = performance.get('PerformanceDate')
        try:
            starts_at = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': url,
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts():
    response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    events = response.json().get('events', [])

    descriptions = {}
    urls = sorted({clean_text(event.get('Link')) for event in events if event.get('Link')})
    # Keep concurrency modest: the origin intermittently rejects larger bursts.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_description, url): url for url in urls}
        for future in as_completed(futures):
            descriptions[futures[future]] = future.result()

    records = []
    for event in events:
        url = clean_text(event.get('Link'))
        records.extend(event_records(event, descriptions.get(url)))

    if not records:
        log_message(
            'No concert performances found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class PhilorchEnsembleArtsPhillyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='philorch_ensembleartsphilly_org',
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
    PhilorchEnsembleArtsPhillyOrgCrawler().run()


if __name__ == '__main__':
    main()
