import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.psnj.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Philharmonic of Southern New Jersey'
TICKET_HOST = 'greatersouthjerseyphilharmonicsocietyinc.thundertix.com'

HEADERS = {
    'Accept': 'text/html,application/xhtml+xml',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def event_urls_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(CONCERTS_URL, link.get('href', ''))
        parsed = urlparse(url)
        if parsed.hostname != TICKET_HOST or not re.fullmatch(r'/events/\d+/?', parsed.path):
            continue
        normalized = f'{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip("/")}'
        if normalized not in urls:
            urls.append(normalized)
    return urls


def event_data_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text(strip=True))
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def record_from_event(event, url):
    title = clean_text(event.get('name'))
    starts_at = event.get('startDate')
    location = event.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country_code = clean_text(address.get('addressCountry')).upper()

    try:
        start = datetime.fromisoformat(starts_at.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None

    if not all((title, venue, city, country_code == 'US')):
        return None

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': clean_text(event.get('url')) or url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    listing_response = session.get(CONCERTS_URL, headers=HEADERS, timeout=60)
    listing_response.raise_for_status()
    event_urls = event_urls_from_html(listing_response.text)

    if not event_urls:
        log_message(
            'No PSNJ ticketed concert links found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
        return []

    records = []
    for url in event_urls:
        try:
            response = session.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            event = event_data_from_html(response.text)
            record = record_from_event(event, url) if event else None
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping PSNJ event with incomplete structured data',
                    event='crawler_record_skipped',
                    level='warning',
                    url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Failed to fetch PSNJ event detail',
                event='crawler_url_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
    )


class PsnjOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='psnj_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PsnjOrgCrawler().run()


if __name__ == '__main__':
    main()
