import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bozemansymphony.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'event-calendar')
SOURCE = 'Bozeman Symphony'
WIDGET_ID = '20cb25d1-e7f3-453d-bc50-7b0c8c8cf74e'
API_URL = 'https://core.service.elfsight.com/p/boot/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_address(address):
    parts = [part.strip() for part in (address or '').split(',') if part.strip()]
    for index, part in enumerate(parts):
        if re.fullmatch(r'MT(?:\s+\d{5})?', part, re.I) and index:
            return re.sub(r'^.*?\d\s+', '', parts[index - 1]).strip()

        match = re.fullmatch(r'(.+?)\s+MT(?:\s+\d{5})?', part, re.I)
        if match:
            return re.sub(r'^.*?\d\s+', '', match.group(1)).strip()
    return ''


def event_url(event):
    actions = event.get('actions') or []
    for action in actions:
        if 'learn' not in clean_text(action.get('text')).lower():
            continue
        link = action.get('link') or {}
        value = link.get('value') or link.get('rawValue')
        if value:
            if not value.startswith(('http://', 'https://')):
                value = f'https://{value.lstrip("/")}'
            return value
    return f'{CALENDAR_URL}#event-{event["id"]}'


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return ''


def detail_description(session, url, cache):
    if not url.startswith(SOURCE_URL) or url.startswith(CALENDAR_URL):
        return None
    if url in cache:
        return cache[url]

    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('main article') or soup.select_one('main')
        cache[url] = clean_text(content) if content else None
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed; using calendar description',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        cache[url] = None
    return cache[url]


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(
        API_URL,
        params={'page': CALENDAR_URL, 'w': WIDGET_ID},
        timeout=45,
    )
    response.raise_for_status()

    try:
        settings = response.json()['data']['widgets'][WIDGET_ID]['data']['settings']
    except (KeyError, TypeError, ValueError) as error:
        log_message(
            'Event calendar API response has an unexpected structure',
            event='crawler_parse_failed',
            level='error',
            url=response.url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    locations = {item['id']: item for item in settings.get('locations', [])}
    records = []
    description_cache = {}
    for event in settings.get('events', []):
        start = event.get('start') or {}
        event_date = valid_date(start.get('date'))
        if not event_date:
            continue

        location_ids = event.get('location') or []
        location = locations.get(location_ids[0]) if location_ids else None
        if not location:
            continue

        venue = clean_text(location.get('name'))
        city = city_from_address(location.get('address'))
        title = clean_text(event.get('name'))
        if not title or not venue or not city:
            continue

        url = event_url(event)
        description = detail_description(session, url, description_cache)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': start.get('time') or None,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or clean_text(event.get('description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No parseable concerts found in event calendar API',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BozemanSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bozemansymphony_org',
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
        return scrape_events()


def main():
    BozemanSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
