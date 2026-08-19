import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.myhso.org/'
SOURCE = 'Hawai\u2018i Symphony Orchestra'
ARCHIVE_URL = urljoin(SOURCE_URL, 'archive')
EVENTS_FEED_URL = (
    'https://app.spektrix-link.com/clients/'
    'hawaiisymphonyorchestra/eventsView.json'
)
EVENT_DETAIL_URL = (
    'https://app.spektrix-link.com/clients/'
    'hawaiisymphonyorchestra/events/{event_number}.json'
)
PURCHASE_URL = 'https://purchase.myhso.org/EventAvailability?EventId={event_id}'
DEFAULT_CITY = 'Honolulu'
HONOLULU_VENUES = {
    'Blaisdell Concert Hall',
    'Hawaii Theatre',
    'Kawaiaha\u02bbo Church',
    'Moanalua High School',
    'Neal S Blaisdell Concert Hall',
    'Neal S. Blaisdell Concert Hall',
    'SALT at our Kaka\u2018ako',
    'Waikiki Shell',
}

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def city_from_location(location):
    address = location.get('address') if isinstance(location, dict) else None
    if isinstance(address, dict):
        return clean_text(address.get('addressLocality'))
    lines = [line.strip() for line in str(address or '').splitlines() if line.strip()]
    for line in lines:
        match = re.match(r'^(.+?),\s*[A-Z]{2}(?:,|\s+\d)', line)
        if match:
            return clean_text(match.group(1))
    return ''


def parse_archive_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return None

    title = clean_text(schema.get('name'))
    title = re.sub(r'\s+[\u2014-]\s+Hawai[\u02bb\u2018\u2019\']i Symphony Orchestra$', '', title)
    event_date, time_from = parse_datetime(schema.get('startDate'))
    location = schema.get('location') or {}
    venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
    city = city_from_location(location)
    if not city and venue in HONOLULU_VENUES:
        city = DEFAULT_CITY
    if not title or not event_date or not venue or not city:
        return None

    content = soup.select_one('.eventitem-column-content')
    description = clean_text(content) or None
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


def archive_record(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_archive_page(url, response.text)


def scrape_archive(session):
    response = session.get(ARCHIVE_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = sorted({
        urljoin(ARCHIVE_URL, link['href'])
        for link in soup.select('a.archive-item-link[href]')
    })

    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(archive_record, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Archive event request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return records


def event_number(event_id):
    match = re.match(r'\d+', event_id or '')
    return match.group() if match else None


def scrape_current(session):
    response = session.get(EVENTS_FEED_URL, timeout=45)
    response.raise_for_status()
    events = response.json()
    records = []

    for event in events:
        number = event_number(event.get('id'))
        if not number:
            continue
        detail_url = EVENT_DETAIL_URL.format(event_number=number)
        try:
            detail_response = session.get(detail_url, timeout=45)
            detail_response.raise_for_status()
            detail = detail_response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Current event request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=detail_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        title = re.sub(r'\s*\|\s*(?:Mon|Tues?|Wed|Thurs?|Fri|Sat|Sun)\b.*$', '', clean_text(detail.get('name')), flags=re.I)
        description = clean_text(detail.get('htmlDescription')) or None
        for instance in detail.get('instances') or []:
            event_date, time_from = parse_datetime(instance.get('start'))
            venue = clean_text((instance.get('availability') or {}).get('name'))
            if not title or not event_date or not venue:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': PURCHASE_URL.format(event_id=detail['id']),
                'time_from': time_from,
                'venue': venue,
                'city': DEFAULT_CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = scrape_archive(session) + scrape_current(session)
    records.sort(key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return records


class MyhsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='myhso_org',
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
    MyhsoOrgCrawler().run()


if __name__ == '__main__':
    main()
