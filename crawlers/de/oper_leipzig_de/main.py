import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oper-leipzig.de/de'
SOURCE = 'Oper Leipzig'
EVENTS_API = f'{SOURCE_URL}/api/events/'
PRODUCTIONS_API = f'{SOURCE_URL}/api/productions/'
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept': 'application/json',
    'Accept-Language': 'de-DE,de;q=0.9',
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=12,
        pool_maxsize=12,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_moment(value):
    if not value:
        return None, None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None, None
    return moment.date().isoformat(), moment.strftime('%H:%M')


def event_url(event):
    slug = event.get('slug') or {}
    slug = slug.get('de') if isinstance(slug, dict) else slug
    production = event.get('production')
    if not slug or not production:
        return None
    return urljoin(f'{SOURCE_URL}/', f'programm/{slug}/{production}')


def venue_name(event):
    stage = event.get('stage_obj') or {}
    return clean_html(
        stage.get('fe_name') or stage.get('display_name') or stage.get('name')
    )


def production_description(production):
    if not production:
        return None
    parts = []
    for key in (
        'headline', 'writer', 'description', 'long_text_2', 'long_text_3',
        'long_text_4', 'long_text_5',
    ):
        value = clean_html(production.get(key))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def fetch_all_events(session):
    # The API defaults to upcoming dates. An early `date` value exposes the
    # complete archive that the current website still publishes.
    url = EVENTS_API
    params = {'date': '2000-01-01'}
    events = []
    while url:
        payload = get_json(session, url, params=params)
        events.extend(payload.get('results') or [])
        url = payload.get('next')
        params = None
    return events


def fetch_production(session, production_id):
    return get_json(session, f'{PRODUCTIONS_API}{production_id}/')


def get_concerts():
    session = make_session()
    events = fetch_all_events(session)
    production_ids = sorted({event.get('production') for event in events if event.get('production')})
    descriptions = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_production, session, production_id): production_id
            for production_id in production_ids
        }
        for future in as_completed(futures):
            production_id = futures[future]
            try:
                descriptions[production_id] = production_description(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Oper Leipzig production',
                    event='crawler_item_failed', level='warning',
                    url=f'{PRODUCTIONS_API}{production_id}/',
                    error_type=type(error).__name__, error_message=str(error),
                )

    records = []
    for event in events:
        title = clean_html(event.get('title'))
        event_date, event_time = parse_moment(event.get('start'))
        venue = venue_name(event)
        url = event_url(event)
        if not title or not event_date or not venue or not url:
            continue
        description = descriptions.get(event.get('production'))
        if not description:
            description = clean_html(event.get('subtitle')) or None
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            # All published stages in this institutional calendar are Leipzig
            # venues, including its explicitly named partner venues.
            'city': 'Leipzig',
            'country_code': 'DE',
            'description': description,
        })

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['venue'], item['title'], item['url'],
    ))


class OperLeipzigDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oper_leipzig_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperLeipzigDeCrawler().run()


if __name__ == '__main__':
    main()
