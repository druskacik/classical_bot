import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rwcmd.ac.uk/'
SOURCE = 'Royal Welsh College of Music & Drama'
LISTING_URL = urljoin(SOURCE_URL, 'whats-on/our-events')
DATE_API_URL = urljoin(SOURCE_URL, 'api/event-dates/get-all')
CITY = 'Cardiff'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def next_data(response):
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    element = soup.select_one('script#__NEXT_DATA__')
    if not element or not element.string:
        raise ValueError('Page does not contain Next.js data')
    return json.loads(element.string)['props']['pageProps']


def local_calendar_date(value):
    # Craft stores calendar dates as local midnight with either +00:00 or
    # +01:00.  Preserve the written calendar day rather than converting UTC.
    return datetime.fromisoformat(value).date().isoformat()


def event_dates(session):
    response = session.get(DATE_API_URL, timeout=45)
    response.raise_for_status()
    dates = set()
    for value in response.json():
        try:
            dates.add(local_calendar_date(value))
        except (TypeError, ValueError):
            continue
    return sorted(dates)


def listing_entries_for_date(date):
    session = requests.Session()
    session.headers.update(HEADERS)
    entries = []
    page = 1
    while True:
        data = next_data(session.get(
            LISTING_URL,
            params={'from': date, 'to': date, 'p': page},
            timeout=45,
        ))['events']
        entries.extend(data.get('entries') or [])
        pagination = data.get('pagination') or {}
        if page >= int(pagination.get('totalPages') or 1):
            break
        page += 1
    return entries


def description_parts(value):
    parts = []
    if isinstance(value, dict):
        body = value.get('body')
        if isinstance(body, str):
            text = clean_text(body)
            if text:
                parts.append(text)
        for key, child in value.items():
            if key != 'body':
                parts.extend(description_parts(child))
    elif isinstance(value, list):
        for child in value:
            parts.extend(description_parts(child))
    return parts


def parse_time(overview):
    values = re.findall(r'(?<!\d)(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', overview or '', re.I)
    parsed = set()
    for hour, minute, meridiem in values:
        hour = int(hour)
        if not 1 <= hour <= 12:
            continue
        if meridiem.lower() == 'pm' and hour != 12:
            hour += 12
        elif meridiem.lower() == 'am' and hour == 12:
            hour = 0
        parsed.add(f'{hour:02d}:{int(minute or 0):02d}')
    return next(iter(parsed)) if len(parsed) == 1 else None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    occurrences = {}
    dates = event_dates(session)
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(listing_entries_for_date, date): date for date in dates}
        for future in as_completed(futures):
            date = futures[future]
            try:
                for entry in future.result():
                    uri = entry.get('uri')
                    if uri:
                        occurrences[(uri, date)] = entry
            except (requests.RequestException, ValueError, KeyError) as error:
                log_message(
                    'Failed to scrape RWCMD listings for date',
                    event='crawler_page_failed',
                    level='warning',
                    url=LISTING_URL,
                    date=date,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    detail_cache = {}
    records = []
    for (uri, date), entry in occurrences.items():
        try:
            if uri not in detail_cache:
                detail_cache[uri] = next_data(session.get(urljoin(SOURCE_URL, uri), timeout=45)).get('entry') or {}
            data = detail_cache[uri]
            merged = dict(entry)
            merged['uri'] = uri
            # Avoid a second request while retaining one parsing path.
            title = clean_text(data.get('title') or merged.get('title'))
            venue_data = data.get('venue') or merged.get('venue') or {}
            venue = clean_text(venue_data.get('text'))
            # RWCMD's events calendar is a Cardiff programme. It includes both
            # College rooms and named partner venues in central Cardiff.
            if not title or not venue:
                continue
            parts = description_parts(data.get('blocks') or [])
            records.append({
                'title': title,
                'date': date,
                'url': urljoin(SOURCE_URL, uri),
                'time_from': parse_time(clean_text(data.get('overview'))),
                'venue': venue,
                'city': CITY,
                'country_code': 'GB',
                'description': '\n\n'.join(dict.fromkeys(parts)) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
        except (requests.RequestException, ValueError, KeyError) as error:
            log_message(
                'Failed to scrape RWCMD event detail',
                event='crawler_item_failed',
                level='warning',
                url=urljoin(SOURCE_URL, uri),
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class RwcmdAcUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rwcmd_ac_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    RwcmdAcUkCrawler().run()


if __name__ == '__main__':
    main()
