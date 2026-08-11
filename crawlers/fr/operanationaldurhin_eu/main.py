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


SOURCE_URL = 'https://www.operanationaldurhin.eu/fr'
SOURCE = 'Opéra national du Rhin'
API_URL = 'https://api.operanationaldurhin.eu'
EVENT_DATES_URL = f'{API_URL}/event_dates'

HEADERS = {
    'Accept': 'application/ld+json, application/json',
    'Accept-Language': 'fr-FR,fr;q=0.9',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

COUNTRY_CODES = {
    'allemagne': 'DE',
    'belgique': 'BE',
    'france': 'FR',
    'suisse': 'CH',
}


def make_session():
    session = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update(HEADERS)
    return session


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n')
    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s*', '', text)
    text = re.sub(r'(?m)^\s*[-*+]\s+', '', text)
    text = re.sub(r'\[([^]]+)]\([^)]*\)', r'\1', text)
    text = text.replace('**', '').replace('__', '').replace('`', '')
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def country_code(value):
    text = clean_text(value)
    if re.fullmatch(r'[A-Za-z]{2}', text):
        return text.upper()
    return COUNTRY_CODES.get(text.lower())


def event_url(event):
    season = event.get('season') or {}
    category = event.get('mainCategory') or {}
    parts = [season.get('slug'), category.get('slug'), event.get('slug')]
    if not all(parts):
        return ''
    return f"{SOURCE_URL}/spectacles/{'/'.join(parts)}"


def parse_occurrence(item, descriptions):
    event = item.get('event') or {}
    place = item.get('place') or {}
    address = place.get('address') or {}
    title = clean_text(event.get('name'))
    url = event_url(event)
    venue = clean_text(place.get('name'))
    city = clean_text(address.get('addressLocality'))
    code = country_code(address.get('addressCountry'))

    try:
        start = datetime.fromisoformat(item.get('doorTime', ''))
    except (TypeError, ValueError):
        return None

    if not all((title, url, venue, city, code)):
        return None

    event_id = event.get('@id')
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': descriptions.get(event_id) or build_description(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def build_description(event):
    review = event.get('review') or {}
    parts = [
        clean_text(event.get('subtitle')),
        clean_text(event.get('excerpt')),
        clean_text(review.get('reviewBody')),
        clean_text(event.get('disclaimer')),
    ]
    unique = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return '\n\n'.join(unique) or None


def fetch_description(event_id):
    session = make_session()
    response = session.get(urljoin(API_URL, event_id), params={'_locale': 'fr'}, timeout=45)
    response.raise_for_status()
    return build_description(response.json())


class OperaNationalDuRhinCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operanationaldurhin_eu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        items = []
        page = 1
        while True:
            response = session.get(
                EVENT_DATES_URL,
                params={
                    '_locale': 'fr',
                    'order[doorTime]': 'ASC',
                    'itemsPerPage': 50,
                    'page': page,
                },
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            members = payload.get('hydra:member', [])
            items.extend(members)
            if not members or not (payload.get('hydra:view') or {}).get('hydra:next'):
                break
            page += 1

        event_ids = sorted({
            item.get('event', {}).get('@id')
            for item in items
            if item.get('event', {}).get('@id')
        })
        descriptions = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_description, event_id): event_id for event_id in event_ids}
            for future in as_completed(futures):
                event_id = futures[future]
                try:
                    descriptions[event_id] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Opéra national du Rhin event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=urljoin(API_URL, event_id),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for item in items:
            record = parse_occurrence(item, descriptions)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Opéra national du Rhin occurrence',
                    event='crawler_item_skipped',
                    level='warning',
                    url=urljoin(API_URL, item.get('@id', '')),
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, city, or country is missing',
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    OperaNationalDuRhinCrawler().run()


if __name__ == '__main__':
    main()
