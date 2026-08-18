import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://thecot.org/'
SOURCE = 'The Chamber Orchestra of the Triangle'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/ajde_events'
CITIES_API_URL = f'{SOURCE_URL}wp-json/wp/v2/event_type_2'

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
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_schema(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_start(value):
    match = re.match(
        r'^(\d{4})-(\d{1,2})-(\d{1,2})(?:T(\d{1,2}):(\d{2}))?',
        value or '',
    )
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    try:
        event_date = date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None
    time_from = f'{int(hour):02d}:{minute}' if hour is not None else None
    return event_date, time_from


def location_name(value):
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, dict)), None)
    if not isinstance(value, dict):
        return ''
    return clean_text(value.get('name'))


def item_to_record(item, schema, cities):
    if not schema:
        return None
    parsed_start = parse_start(schema.get('startDate'))
    title = clean_text(schema.get('name') or (item.get('title') or {}).get('rendered'))
    url = schema.get('url') or item.get('link')
    venue = location_name(schema.get('location'))
    city_ids = item.get('event_type_2') or []
    city = next((cities.get(city_id) for city_id in city_ids if cities.get(city_id)), '')
    if not parsed_start or not title or not url or not venue or not city:
        return None

    description = clean_text((item.get('content') or {}).get('rendered'))
    if not description:
        description = clean_text(schema.get('description'))
    event_date, time_from = parsed_start
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
    }


def get_all_pages(session, url, params):
    records = []
    page = 1
    while True:
        response = session.get(url, params={**params, 'page': page}, timeout=45)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f'Expected a list from {url}')
        records.extend(payload)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return records
        page += 1


class ChamberOrchestraOfTheTriangleOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chamberorchestraofthetriangle_org',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            city_terms = get_all_pages(
                session,
                CITIES_API_URL,
                {'per_page': 100, '_fields': 'id,name'},
            )
            items = get_all_pages(
                session,
                EVENTS_API_URL,
                {
                    'per_page': 100,
                    '_fields': 'id,link,title,content,event_type,event_type_2',
                },
            )
            cities = {term['id']: clean_text(term.get('name')) for term in city_terms}

            def fetch_schema(item):
                response = session.get(item['link'], timeout=45)
                response.raise_for_status()
                return item, event_schema(response.text)

            with ThreadPoolExecutor(max_workers=6) as executor:
                parsed_items = list(executor.map(fetch_schema, items))
        except (requests.RequestException, ValueError, KeyError) as error:
            log_message(
                'Failed to fetch Chamber Orchestra of the Triangle events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for item, schema in parsed_items:
            record = item_to_record(item, schema, cities)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete date or location',
                    event='crawler_record_skipped',
                    level='warning',
                    url=item.get('link'),
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ChamberOrchestraOfTheTriangleOrgCrawler().run()


if __name__ == '__main__':
    main()
