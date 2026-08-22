import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mozart.co.at/'
PROGRAM_API = 'https://mozart.co.at/wp-admin/admin-ajax.php'
SOURCE = 'Wiener Mozart Orchester'
CITY = 'Wien'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-AT,de;q=0.9,en;q=0.7',
}

VENUES = {
    'brahmssaal': 'Brahms-Saal, Musikverein Wien',
    'goldener saal': 'Goldener Saal, Musikverein Wien',
    'hofburg': 'Hofburg Wien',
    'palais auersperg': 'Palais Auersperg',
    'staatsoper': 'Wiener Staatsoper',
}


def clean_html(value):
    if not value:
        return None
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if not text or re.fullmatch(r'(?:No program|Kein Programm)(?: for this day)?\.?', text, re.I):
        return None
    return text


def calendar_payload(html):
    match = re.search(r'\bvar\s+atts\s*=\s*', html)
    if not match:
        raise ValueError('Calendar data variable was not found')
    payload, _ = json.JSONDecoder().raw_decode(html[match.end():])
    if not isinstance(payload.get('events'), list):
        raise ValueError('Calendar events are missing')
    return payload


def fetch_program(event):
    params = {
        'action': 'calendar_get_program',
        'postId': event['extendedProps']['postId'],
        'date': event['start'],
        'locale': 'de',
    }
    response = requests.get(PROGRAM_API, params=params, headers=HEADERS, timeout=45)
    response.raise_for_status()
    payload = response.json()
    if not payload.get('success'):
        return None
    return clean_html((payload.get('data') or {}).get('html'))


def start_time(description):
    if not description:
        return None
    patterns = (
        r'(\d{1,2})[:.]([0-5]\d)\s*Uhr\s*[–-]\s*Konzertbeginn',
        r'Konzertbeginn\s*(?:um\s*)?(\d{1,2})[:.]([0-5]\d)',
    )
    for pattern in patterns:
        match = re.search(pattern, description, re.I)
        if match:
            return f'{int(match.group(1)):02d}:{match.group(2)}'
    return None


def make_record(event, description):
    title = str(event.get('title') or '').strip()
    date_value = str(event.get('start') or '')[:10]
    props = event.get('extendedProps') or {}
    venue_value = str(props.get('saal') or '').strip()
    try:
        date_value = date.fromisoformat(date_value).isoformat()
    except ValueError:
        return None
    venue = VENUES.get(venue_value.casefold())
    if not title or not venue:
        return None
    query = urlencode({'concert_date': date_value, 'concert': title})
    return {
        'title': title,
        'date': date_value,
        'url': f'{SOURCE_URL}?{query}',
        'time_from': start_time(description),
        'venue': venue,
        'city': CITY,
        'country_code': 'AT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MozartCoAtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mozart_co_at',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=90)
        response.raise_for_status()
        events = calendar_payload(response.text)['events']
        unique_events = {}
        for event in events:
            props = event.get('extendedProps') or {}
            key = (event.get('start'), event.get('title'), props.get('postId'), props.get('saal'))
            unique_events[key] = event

        descriptions = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_program, event): key for key, event in unique_events.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    descriptions[key] = future.result()
                except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as error:
                    log_message(
                        'Failed to scrape concert programme',
                        event='crawler_item_failed',
                        level='warning',
                        url=f'{PROGRAM_API}?date={key[0]}',
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    descriptions[key] = None

        records = []
        for key, event in unique_events.items():
            record = make_record(event, descriptions.get(key))
            if record:
                records.append(record)
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    MozartCoAtCrawler().run()


if __name__ == '__main__':
    main()
