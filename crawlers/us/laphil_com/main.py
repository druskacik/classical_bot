import re
import time
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.laphil.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'tickets-and-events/calendar')
API_URL = urljoin(SOURCE_URL, 'api/event-instances.json')
SOURCE = 'LA Phil'
CITY = 'Los Angeles'
COUNTRY_CODE = 'US'
TIMEZONE = ZoneInfo('America/Los_Angeles')
PAGE_SIZE = 20
MAX_RETRIES = 4

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
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


def local_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(TIMEZONE)
    except (TypeError, ValueError):
        return None


def program_text(event):
    custom = (event.get('attributes') or {}).get('custom') or {}
    lines = []
    for section in custom.get('program') or []:
        heading = clean_text(section.get('heading'))
        if heading:
            lines.append(heading)
        for item in section.get('pieces') or []:
            piece = item.get('piece') or {}
            if not isinstance(piece, dict):
                continue
            piece_custom = (piece.get('attributes') or {}).get('custom') or {}
            title = clean_text(piece.get('richTitle_html') or piece.get('title'))
            composers = []
            for creator in piece.get('creator') or []:
                if isinstance(creator, dict):
                    name = clean_text(creator.get('name'))
                    if name and name not in composers:
                        composers.append(name)
            for composer in piece_custom.get('composers') or []:
                artist = composer.get('artist') or {}
                if isinstance(artist, dict):
                    name = clean_text(artist.get('name'))
                    if name and name not in composers:
                        composers.append(name)
            line = ' — '.join(part for part in [', '.join(composers), title] if part)
            if line and line.lower() != 'intermission':
                lines.append(line)
    return '\n'.join(lines)


def description_text(event):
    custom = (event.get('attributes') or {}).get('custom') or {}
    parts = []
    for value in (
        event.get('richDescription_html'),
        event.get('description'),
        custom.get('description_html'),
        program_text(event),
    ):
        text = clean_text(value)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def fetch_page(session, page):
    params = {'depth': 2, 'limit': PAGE_SIZE, 'page': page}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(API_URL, params=params, timeout=60)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            if attempt == MAX_RETRIES:
                log_message(
                    'LA Phil API page failed',
                    event='crawler_api_page_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            time.sleep(attempt)
    return {}


def record_from_instance(instance):
    event = instance.get('event') or {}
    venue = event.get('venue') or {}
    title = clean_text(event.get('richTitle_html') or event.get('title'))
    venue_name = clean_text(venue.get('title')) if isinstance(venue, dict) else ''
    start = local_datetime(instance.get('startDate'))
    path = instance.get('url')
    url = urljoin(SOURCE_URL, path) if path else ''
    if not title or not venue_name or not start or not url.startswith(SOURCE_URL):
        return None

    hide_time = bool(instance.get('hideStartTime') or event.get('hideStartTime'))
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if hide_time else start.strftime('%H:%M'),
        'venue': venue_name,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': description_text(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1
    while True:
        payload = fetch_page(session, page)
        for instance in payload.get('docs') or []:
            record = record_from_instance(instance)
            if record:
                records.append(record)
        if not payload.get('hasNextPage'):
            break
        page += 1

    if not records:
        log_message(
            'No LA Phil event instances found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class LaPhilComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='laphil_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    LaPhilComCrawler().run()


if __name__ == '__main__':
    main()
