import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatromassimo.it/'
CALENDAR_URL = f'{SOURCE_URL}calendario/'
SOURCE = 'Teatro Massimo di Palermo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

EVENTS_RE = re.compile(r'\blet\s+events\s*=\s*(\[.*?\]);\s*(?:let|const|var)\s', re.S)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def embedded_events(html):
    match = EVENTS_RE.search(html)
    if not match:
        raise ValueError('Calendar event payload was not found')
    events = json.loads(match.group(1))
    if not isinstance(events, list):
        raise ValueError('Calendar event payload is not a list')
    return events


def detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article')
    return clean_text(article) or None


def location_for(place):
    venue = clean_text(place)
    folded = venue.casefold()
    if not venue or folded == 'teatri in giappone':
        return None
    if folded == 'ho guom opera':
        return venue, 'Hanoi', 'VN'
    if 'taormina' in folded:
        return venue, 'Taormina', 'IT'
    if 'catania' in folded:
        return venue, 'Catania', 'IT'
    if 'monreale' in folded:
        return venue, 'Monreale', 'IT'
    return venue, 'Palermo', 'IT'


def occurrence_record(event, description):
    try:
        event_date = date(int(event['year']), int(event['month']), int(event['day'])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    title = clean_text(event.get('title'))
    url = clean_text(event.get('permalink'))
    location = location_for(event.get('place'))
    if not title or not url.startswith(SOURCE_URL) or location is None:
        return None

    time_from = clean_text(event.get('time')) or None
    if time_from and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
        time_from = None

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class TeatromassimoItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatromassimo_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = embedded_events(fetch(session, CALENDAR_URL))
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            log_message(
                'Failed to fetch Teatro Massimo calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = sorted({clean_text(item.get('permalink')) for item in events})
        urls = [url for url in urls if url.startswith(SOURCE_URL)]
        descriptions = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = detail_description(future.result())
                except (requests.RequestException, ValueError) as error:
                    descriptions[url] = None
                    log_message(
                        'Failed to fetch Teatro Massimo event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for event in events:
            url = clean_text(event.get('permalink'))
            record = occurrence_record(event, descriptions.get(url))
            if record:
                records.append(record)
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    TeatromassimoItCrawler().run()


if __name__ == '__main__':
    main()
