import calendar
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.aspenmusicfestival.com/'
SOURCE = 'Aspen Music Festival and School'
CALENDAR_API = urljoin(SOURCE_URL, '_script/events.calendar/list-cal/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Nearly all AMFS venues are in Aspen. These are the two clearly named
# exceptions in the first-party calendar.
VENUE_CITIES = {
    'Anderson Ranch Arts Center': 'Snowmass Village',
    'Basalt Regional Library': 'Basalt',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    else:
        value = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    value = value.replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def clean_inline(value):
    return re.sub(r'\s+', ' ', clean_text(value)).strip()


def parse_date(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date().isoformat()
    except (TypeError, ValueError):
        return ''


def parse_time(value):
    for pattern in ('%I:%M %p', '%I %p', '%H:%M'):
        try:
            return datetime.strptime(clean_text(value).upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def calendar_url(year, month):
    month_name = calendar.month_name[month]
    return f'{CALENDAR_API}{year}-{month_name}/?date={month}/1/{str(year)[2:]}'


def fetch_month(session, year, month):
    url = calendar_url(year, month)
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError('calendar response is not a list')
        return payload
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Calendar month request failed',
            event='crawler_month_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []


def fetch_description(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        node = soup.select_one('.event-program-detail') or soup.select_one('.program')
        text = clean_text(node)
        lines = [line for line in text.splitlines()
                 if not re.fullmatch(r'\d{2}|PROGRAM|FEATURED ARTISTS', line, re.I)]
        return '\n'.join(lines).strip() or None
    except requests.RequestException as error:
        log_message(
            'Event detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


def api_description(item):
    parts = []
    for value in (item.get('short_description'), item.get('program')):
        text = clean_text(value)
        if text and text not in parts:
            parts.append(text)
    performers = []
    for performer in item.get('performers_new') or []:
        name = clean_text(performer.get('name'))
        instrument = clean_text(performer.get('instrument'))
        if name:
            performers.append(f'{name}, {instrument}' if instrument else name)
    if performers:
        parts.append('Featured artists:\n' + '\n'.join(performers))
    return '\n\n'.join(parts) or None


def scrape_concerts(session=None, today=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    today = today or date.today()

    items = []
    # AMFS is a summer festival. The endpoint returns an empty list for
    # unavailable years; checking the adjacent summers captures retained
    # archives and early releases without issuing dozens of empty requests.
    for year in range(today.year - 1, today.year + 2):
        july_items = fetch_month(session, year, 7)
        if not july_items:
            continue
        items.extend(july_items)
        for month in (6, 8, 9):
            items.extend(fetch_month(session, year, month))

    candidates = []
    seen = set()
    for item in items:
        event_date = parse_date(item.get('event_date'))
        venue = clean_inline(item.get('venue'))
        relative_url = clean_inline(item.get('url'))
        title = clean_inline(item.get('event_title')) or clean_inline(item.get('title'))
        if not event_date or not title or not venue or not relative_url:
            continue
        # Online-only broadcasts are not live concert occurrences and cannot be
        # assigned a defensible city.
        if venue.casefold() == 'amfs virtual stage':
            continue
        url = urljoin(SOURCE_URL, relative_url)
        city = VENUE_CITIES.get(venue, 'Aspen')
        key = (title, event_date, parse_time(item.get('start_time')), venue, url)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((item, {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': key[2],
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': api_description(item),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }))

    descriptions = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_description, url): url
                   for url in {record['url'] for _, record in candidates}}
        for future in as_completed(futures):
            descriptions[futures[future]] = future.result()

    records = []
    for _, record in candidates:
        record['description'] = descriptions.get(record['url']) or record['description']
        records.append(record)

    if not records:
        log_message(
            'No calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_API,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class AspenMusicFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aspenmusicfestival_com',
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
    AspenMusicFestivalComCrawler().run()


if __name__ == '__main__':
    main()
