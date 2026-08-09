import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sing-akademie.ch/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Zürcher Sing-Akademie'

HEADERS = {
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
    'Referer': SOURCE_URL,
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

VENUE_COUNTRY_MARKERS = {
    'B': 'BE',
    'D': 'DE',
    'E': 'ES',
    'F': 'FR',
    'JP': 'JP',
    'LU': 'LU',
    'NL': 'NL',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = re.sub(r'\[(?:/?vc_[^\]]+|/?vc_tabs[^\]]*|/?vc_tab[^\]]*)\]', '\n', text)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'asc'},
            timeout=60,
        )
        response.raise_for_status()
        events.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', 1)):
            return events
        page += 1


def event_details(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    time_node = soup.select_one('.mec-single-event-time')
    time_match = re.search(r'\b(?:[01]\d|2[0-3]):[0-5]\d\b', clean_text(time_node))
    displayed_time = time_match.group(0) if time_match else None
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload.get('@graph', []) if isinstance(payload, dict) else []
        if isinstance(payload, dict):
            candidates.append(payload)
        for candidate in candidates:
            if candidate.get('@type') == 'Event':
                return candidate, displayed_time
    return None, displayed_time


def parse_city(address):
    city = clean_text(address).rsplit(',', 1)[-1].strip()
    city = re.sub(r'^\d{4,6}\s+', '', city)
    if city == 'Clausen Luxembourg':
        return 'Luxembourg'
    return city


def parse_country_code(venue):
    match = re.search(r'\(([A-Z]{1,2})\)\s*$', venue)
    if match:
        return VENUE_COUNTRY_MARKERS.get(match.group(1))
    return 'CH'


def parse_event(event, page_html):
    schema, displayed_time = event_details(page_html)
    if not schema:
        return None

    title = clean_text((event.get('title') or {}).get('rendered'))
    url = event.get('link') or ''
    location = schema.get('location') or {}
    venue = clean_text(location.get('name'))
    city = parse_city(location.get('address'))
    country_code = parse_country_code(venue)
    start = schema.get('startDate') or ''
    if not all((title, url, venue, city, country_code, start)):
        return None

    try:
        start_at = datetime.fromisoformat(start)
    except ValueError:
        return None

    time_from = displayed_time
    description = clean_text((event.get('content') or {}).get('rendered')) or None
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


class SingAkademieChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sing_akademie_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Zürcher Sing-Akademie event catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, event.get('link', ''), timeout=60): event
                for event in events if event.get('link')
            }
            for future in as_completed(futures):
                event = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_event(event, response.text)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Zürcher Sing-Akademie event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=event.get('link'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped Zürcher Sing-Akademie event with incomplete data',
                        event='crawler_item_skipped',
                        level='warning',
                        url=event.get('link'),
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SingAkademieChCrawler().run()


if __name__ == '__main__':
    main()
