from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gsmd.ac.uk/'
EVENTS_URL = f'{SOURCE_URL}whats-on'
API_URL = f'{SOURCE_URL}jsonapi/index/site'
SOURCE = 'Guildhall School of Music & Drama'
CITY = 'London'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

API_PARAMS = {
    'page[limit]': 50,
    'filter[prefilter_type-filter][condition][path]': 'prefilter_type',
    'filter[prefilter_type-filter][condition][operator]': 'IN',
    'filter[prefilter_type-filter][condition][value][]': 'event',
    'sort[event_date][path]': 'event_date',
    'sort[event_date][direction]': 'asc',
    'sort[field_start_time][path]': 'field_start_time',
    'sort[field_start_time][direction]': 'asc',
    'fields[node--event]': (
        'path,title,field_teaser_summary,field_date_from,field_date_to,'
        'field_start_time,field_time,field_location,field_cancelled_postponed'
    ),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def api_events(session):
    url = API_URL
    params = API_PARAMS
    events = []
    while url:
        payload = get_json(session, url, params=params)
        events.extend(payload.get('data', []))
        url = payload.get('links', {}).get('next', {}).get('href')
        params = None
    return events


def teaser_text(attributes):
    teaser = attributes.get('field_teaser_summary') or {}
    return clean_text(teaser.get('processed') or teaser.get('value')) or None


def detail_description(session, url, fallback):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    sections = []
    for heading in soup.select('main h2'):
        label = clean_text(heading).lower()
        if label not in {'event information', 'programme & performers'}:
            continue
        section = heading.find_parent('section')
        text = clean_text(section)
        if text and text not in sections:
            sections.append(text)
    return '\n\n'.join(sections) or fallback


def event_dates(attributes):
    try:
        start = date.fromisoformat(attributes['field_date_from'])
        end = date.fromisoformat(attributes.get('field_date_to') or start.isoformat())
    except (KeyError, TypeError, ValueError):
        return []
    if end < start or (end - start).days > 31:
        return []
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def start_time(value):
    if not isinstance(value, int) or value < 0 or value >= 24 * 60 * 60:
        return None
    hours, remainder = divmod(value, 3600)
    minutes = remainder // 60
    return f'{hours:02d}:{minutes:02d}'


def event_stub(item):
    attributes = item.get('attributes', {})
    alias = (attributes.get('path') or {}).get('alias')
    title = clean_text(attributes.get('title'))
    venue = clean_text(attributes.get('field_location'))
    dates = event_dates(attributes)
    if not alias or not title or not venue or not dates:
        return None
    return {
        'title': title,
        'dates': dates,
        'url': requests.compat.urljoin(SOURCE_URL, alias),
        'time_from': start_time(attributes.get('field_start_time')),
        'venue': venue,
        'description': teaser_text(attributes),
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    stubs = [stub for item in api_events(session) if (stub := event_stub(item))]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, stub['url'], stub['description']): stub
            for stub in stubs
        }
        for future in as_completed(futures):
            stub = futures[future]
            try:
                stub['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Guildhall event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=stub['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for stub in stubs:
        for event_date in stub.pop('dates'):
            records.append(
                {
                    **stub,
                    'date': event_date,
                    'city': CITY,
                    'country_code': 'GB',
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                }
            )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class GsmdAcUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gsmd_ac_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    GsmdAcUkCrawler().run()


if __name__ == '__main__':
    main()
