import json
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.esplanade.com/'
SOURCE = 'Esplanade – Theatres on the Bay'
SEARCH_URL = (
    'https://edge-platform.sitecorecloud.io/v1/search'
    '?sitecoreContextId=1PkquNc51VRGK9OcVwwYZu'
)
PAGE_SIZE = 100
SINGAPORE_TZ = ZoneInfo('Asia/Singapore')

HEADERS = {
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Referer': SOURCE_URL,
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def search_payload(offset):
    # Music and Dance are stable first-party discipline values. The narrower
    # Classical Music / Opera tag misses eligible orchestral crossover, film
    # score, contemporary-music, family, and classical-dance performances, so
    # the complete candidate feed is sent through potential-event review.
    return {
        'context': {'locale': {'country': 'us', 'language': 'en'}},
        'widget': {'items': [{
            'entity': 'espevententity',
            'rfk_id': 'rfkid_7',
            'sources': ['events_web_crawler'],
            'search': {
                'offset': offset,
                'sort': {'value': [{'name': 'event_startdate_timestamp_ascending'}]},
                'limit': PAGE_SIZE,
                'filter': {
                    'filters': [
                        {'name': 'type', 'type': 'eq', 'value': 'Event'},
                        {
                            'filters': [
                                {'name': 'event_category', 'type': 'eq', 'value': 'Music'},
                                {'name': 'event_category', 'type': 'eq', 'value': 'Dance'},
                            ],
                            'type': 'or',
                        },
                    ],
                    'type': 'and',
                },
                'content': {},
            },
        }]},
    }


def fetch_candidates(session):
    candidates = []
    offset = 0
    total = None
    while total is None or offset < total:
        response = session.post(SEARCH_URL, json=search_payload(offset), timeout=60)
        response.raise_for_status()
        payload = response.json()
        widgets = payload.get('widgets') or []
        if not widgets:
            raise ValueError('Sitecore Search returned no event widget')
        widget = widgets[0]
        page = widget.get('content') or []
        total = int(widget.get('total_item') or 0)
        candidates.extend(page)
        if not page:
            break
        offset += len(page)
    return candidates


def detail_description(session, url, fallback):
    try:
        response = session.get(url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        node = soup.select_one('script#__NEXT_DATA__')
        if not node:
            return fallback or None
        data = json.loads(node.string or node.get_text())
        route = data['props']['pageProps']['page']['layout']['sitecore']['route']
        fields = route.get('fields') or {}
        parts = []
        for name in ('Full Synopsis', 'Content'):
            value = clean_text((fields.get(name) or {}).get('value'))
            if value and value not in parts:
                parts.append(value)
        return '\n\n'.join(parts) or fallback or None
    except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        log_message(
            'Failed to fetch Esplanade event detail',
            event='crawler_detail_fetch_failed', level='warning', url=url,
            error_type=type(error).__name__, error_message=str(error),
        )
        return fallback or None


def occurrence_datetimes(event):
    values = event.get('event_time') or []
    raw_values = []
    for value in values:
        raw_values.extend(str(value).split(','))
    occurrences = []
    for value in raw_values:
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
            occurrences.append(parsed.astimezone(SINGAPORE_TZ))
        except (TypeError, ValueError):
            continue
    return occurrences


def event_records(session, event):
    title = clean_text(event.get('name'))
    path = clean_text(event.get('url'))
    venues = [clean_text(item) for item in event.get('event_venue') or []]
    venues = [item for item in venues if item]
    occurrences = occurrence_datetimes(event)
    if not title or not path or len(venues) != 1 or not occurrences:
        return []
    venue = venues[0]
    if venue.casefold() in {'online', 'various locations', 'various venues'}:
        return []
    url = urljoin(SOURCE_URL, path)
    fallback = clean_text(event.get('description')) or None
    description = detail_description(session, url, fallback)
    return [{
        'title': title,
        'date': occurrence.date().isoformat(),
        'url': url,
        'time_from': occurrence.strftime('%H:%M'),
        'venue': venue,
        'city': 'Singapore',
        'country_code': 'SG',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for occurrence in occurrences]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        candidates = fetch_candidates(session)
    except (requests.RequestException, ValueError, TypeError) as error:
        log_message(
            'Failed to fetch Esplanade candidate feed',
            event='crawler_fetch_failed', level='error', url=SEARCH_URL,
            error_type=type(error).__name__, error_message=str(error),
        )
        raise

    records = []
    for event in candidates:
        records.extend(event_records(session, event))
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class EsplanadeComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='esplanade_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SG',
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
    EsplanadeComCrawler().run()


if __name__ == '__main__':
    main()
