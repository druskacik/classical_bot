import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://theater.freiburg.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'de_DE/spielplan')
API_URL = urljoin(SOURCE_URL, 'de_DE/event.json')
SOURCE = 'Theater Freiburg'
CITY = 'Freiburg im Breisgau'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def calendar_events(session):
    events = []
    # An early date asks the API for the complete archive it still exposes.
    # Without it, the endpoint starts at the next upcoming performance.
    for page in range(1, 201):
        response = session.get(
            API_URL,
            params={
                'm': 'calendar_events',
                'p': page,
                'fields[date]': '2000-01-01',
                'fields[category]': 'all',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        groups = payload.get('Results')
        if not isinstance(groups, list):
            raise ValueError(f'Unexpected calendar response on page {page}')
        for group in groups:
            group_events = group.get('Events', [])
            if isinstance(group_events, list):
                events.extend(group_events)
        if payload.get('IsLastPage'):
            return events
    raise ValueError('Calendar pagination exceeded 200 pages')


def event_venue(event):
    soup = BeautifulSoup(event.get('TimeLocation') or '', 'html.parser')
    venue_node = soup.select_one('div')
    return clean_text(venue_node)


def event_record(event):
    title = clean_text(event.get('Title'))
    relative_url = event.get('Slug') or ''
    venue = event_venue(event)
    try:
        event_date = date.fromisoformat(str(event.get('Date'))).isoformat()
    except ValueError:
        return None
    if not title or not relative_url or not venue:
        return None

    start_time = None
    try:
        start_time = datetime.fromisoformat(event.get('DateTimeAtom', '')).strftime('%H:%M')
    except (TypeError, ValueError):
        pass

    summary_parts = [
        clean_text(event.get(field))
        for field in ('Advertising', 'OpusInfoShort', 'ProgramBook')
    ]
    description = '\n\n'.join(dict.fromkeys(x for x in summary_parts if x)) or None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, relative_url),
        'time_from': start_time,
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    parts = []
    for selector in (
        '.event__description-text',
        '.event__coproduction',
        '.event__ensemble-cast',
        '.event__ensemble',
        '.event_statement',
    ):
        for node in soup.select(selector):
            value = clean_text(node)
            if value and value not in parts:
                parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = make_session()
    records = [
        record for event in calendar_events(session)
        if (record := event_record(event))
    ]

    # Production descriptions are shared by all occurrences, so fetch each
    # production page once while preserving occurrence-specific URLs below.
    detail_urls = {}
    for record in records:
        path = urlsplit(record['url']).path
        detail_urls.setdefault(path, record['url'])

    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, url): path
            for path, url in detail_urls.items()
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                descriptions[path] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Theater Freiburg event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=detail_urls[path],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(urlsplit(record['url']).path)
        if detail:
            summary = record.get('description')
            record['description'] = '\n\n'.join(
                dict.fromkeys(part for part in (summary, detail) if part)
            )

    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['venue'], item['title'], item['url']
    ))


class TheaterFreiburgDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_freiburg_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterFreiburgDeCrawler().run()


if __name__ == '__main__':
    main()
