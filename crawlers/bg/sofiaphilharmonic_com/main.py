import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sofiaphilharmonic.com/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/sabitia'
SOURCE = 'Sofia Philharmonic'
VENUE = 'Concert Complex Bulgaria'
CITY = 'Sofia'

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    value = html.unescape(str(value))
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True) if '<' in value else value
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_fields(event):
    groups = event.get('toolset-meta') or {}
    for group in groups.values():
        if isinstance(group, dict) and 'sb_nachdata' in group:
            return group
    return {}


def field_value(fields, name, key='raw'):
    field = fields.get(name) or {}
    return field.get(key) if isinstance(field, dict) else None


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%d.%m.%Y %H:%M')
    except (TypeError, ValueError):
        return None


def make_record(event, works=None):
    fields = event_fields(event)
    # Touring events cannot safely inherit the venue calendar's Sofia location.
    if field_value(fields, 'turneta'):
        return None

    start = parse_datetime(field_value(fields, 'sb_nachdata', 'formatted'))
    end = parse_datetime(field_value(fields, 'sab_krdata', 'formatted'))
    title = clean_text((event.get('title') or {}).get('rendered'))
    url = clean_text(event.get('link'))
    if not title or not url or not start:
        return None

    description_parts = [
        clean_text(field_value(fields, 'sb_podzaglavie')),
        clean_text(field_value(fields, 'sb_opisanie')),
    ]
    work_ids = (event.get('acf') or {}).get('acf-proizvedenia-relationship') or []
    programme = [works[work_id] for work_id in work_ids if works and work_id in works]
    if programme:
        description_parts.append('Програма\n' + '\n'.join(programme))
    description = '\n\n'.join(part for part in description_parts if part) or None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'time_to': end.strftime('%H:%M') if end else None,
        'venue': VENUE,
        'city': CITY,
        'description': description,
    }


def get_page(session, page):
    for attempt in range(1, 4):
        try:
            response = session.get(
                EVENTS_API,
                params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'desc'},
                timeout=90,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            if attempt == 3:
                raise
            log_message(
                'Retrying Sofia Philharmonic API page',
                event='crawler_page_retry',
                level='warning',
                url=EVENTS_API,
                page=page,
                attempt=attempt,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            time.sleep(attempt)


def get_works(session, work_ids):
    works = {}
    ordered_ids = sorted(work_ids)
    for offset in range(0, len(ordered_ids), 100):
        batch = ordered_ids[offset:offset + 100]
        for attempt in range(1, 4):
            try:
                response = session.get(
                    f'{SOURCE_URL}wp-json/wp/v2/proizvedenia',
                    params={'include': ','.join(map(str, batch)), 'per_page': 100},
                    timeout=90,
                )
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(attempt)
        for work in response.json():
            title = clean_text((work.get('title') or {}).get('rendered'))
            if title:
                works[work['id']] = title
    return works


def scrape_events():
    session = requests.Session()
    session.headers.update(HEADERS)

    first_response = get_page(session, 1)
    events = first_response.json()
    total_pages = int(first_response.headers.get('X-WP-TotalPages', '1'))

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(get_page, session, page): page for page in range(2, total_pages + 1)}
        for future in as_completed(futures):
            page = futures[future]
            try:
                events.extend(future.result().json())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Sofia Philharmonic API page',
                    event='crawler_page_failed',
                    level='error',
                    url=EVENTS_API,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

    work_ids = {
        work_id
        for item in events
        for work_id in ((item.get('acf') or {}).get('acf-proizvedenia-relationship') or [])
    }
    works = get_works(session, work_ids)

    records = [record for item in events if (record := make_record(item, works))]
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class SofiaPhilharmonicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sofiaphilharmonic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BG',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to',
            'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_events()


def main():
    SofiaPhilharmonicComCrawler().run()


if __name__ == '__main__':
    main()
