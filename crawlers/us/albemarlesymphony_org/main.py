import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.albemarlesymphony.org/'
EVENT_SITEMAP_URL = f'{SOURCE_URL}event-pages-sitemap.xml'
SOURCE = 'Albemarle Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

ADDRESS_CITY_RE = re.compile(
    r',\s*([^,]+?),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?(?:,\s*USA)?\s*$', re.I
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def event_urls(xml_text):
    soup = BeautifulSoup(xml_text, 'xml')
    return sorted({
        clean_text(node)
        for node in soup.find_all('loc')
        if '/event-details/' in clean_text(node)
    })


def event_json(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def location_fields(location):
    venue = clean_text(location.get('name'))
    address = clean_text(location.get('address'))
    city_match = ADDRESS_CITY_RE.search(address)
    city = clean_text(city_match.group(1)) if city_match else ''

    # Wix stored one Grisham Hall event with the city as its venue name. The
    # street address is the same first-party address used by the other Grisham
    # Hall events, so the hall can be safely restored here.
    if address.startswith('2132 Ivy Rd, Charlottesville,'):
        city = 'Charlottesville'
        if not venue or venue.lower() == city.lower():
            venue = 'Grisham Hall - St. Anne\'s-Belfield School'
    elif 'Crozet Baptist Church' in f'{venue} {address}':
        city = city or 'Crozet'
        venue = venue or 'Crozet Baptist Church'

    return venue, city


def parse_event_page(url, html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    data = event_json(soup)
    if not data:
        return None

    title = clean_text(data.get('name'))
    event_date, time_from = parse_datetime(data.get('startDate'))
    location = data.get('location') if isinstance(data.get('location'), dict) else {}
    venue, city = location_fields(location)
    if not all((title, event_date, venue, city)):
        return None

    description_parts = []
    summary = clean_text(data.get('description'))
    if summary:
        description_parts.append(summary)
    about = soup.select_one('[data-hook="about-section"]')
    if about:
        about_text = clean_text(about)
        about_text = re.sub(r'^About the event\s*', '', about_text, flags=re.I)
        about_text = re.sub(r'\s*Show More\s*$', '', about_text, flags=re.I)
        if about_text and about_text not in description_parts:
            description_parts.append(about_text)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENT_SITEMAP_URL, timeout=45)
    response.raise_for_status()
    urls = event_urls(response.text)

    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                detail_response = future.result()
                detail_response.raise_for_status()
                record = parse_event_page(url, detail_response.text)
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                log_message(
                    'Event detail did not contain required fields',
                    event='crawler_detail_skipped',
                    level='warning',
                    url=url,
                )

    if not records:
        log_message(
            'No event records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENT_SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class AlbemarleSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='albemarlesymphony_org',
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
    AlbemarleSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
