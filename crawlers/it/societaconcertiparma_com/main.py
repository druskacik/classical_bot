import html
import json
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.societaconcertiparma.com/'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/mec-events')
SOURCE = 'Società dei Concerti di Parma'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

CATEGORY_VENUES = {
    62: 'Casa della Musica',
    68: 'Casa della Musica',
    69: 'Teatro Regio di Parma',
    84: 'Teatro Regio di Parma',
}

VENUE_PATTERNS = [
    (r'Fondazione Magnani[\s-]*Rocca', 'Fondazione Magnani-Rocca', 'Mamiano di Traversetolo'),
    (r'Teatro Regio(?: di Parma)?', 'Teatro Regio di Parma', 'Parma'),
    (r'Teatro Farnese', 'Teatro Farnese', 'Parma'),
    (r'Auditorium(?: Niccolò )?Paganini(?: di Parma)?', 'Auditorium Paganini', 'Parma'),
    (r'Auditorium del Carmine', 'Auditorium del Carmine', 'Parma'),
    (r'Duomo di Parma', 'Duomo di Parma', 'Parma'),
    (r'Chiesa di San Francesco del Prato', 'Chiesa di San Francesco del Prato', 'Parma'),
    (r'Cortile d[\u2019\']Onore(?: presso )?Casa della Musica', 'Cortile d\u2019Onore della Casa della Musica', 'Parma'),
    (r'(?:Sala Concerti\s*[|\u2013-]\s*)?Casa della Musica', 'Casa della Musica', 'Parma'),
]


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def get_events(session):
    events = []
    page = 1
    while True:
        response = get_response(session, API_URL, {'per_page': 100, 'page': page})
        events.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return events
        page += 1


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def parse_time(soup, description):
    match = re.search(
        r'(?:\bore\s*|\b\d{1,2}\s+[a-zà]+ ?(?:\s+\d{4})?\s*,?\s*)'
        r'([01]?\d|2[0-3])[:.]([0-5]\d)\b',
        description,
        re.I,
    )
    if match:
        return f'{int(match.group(1)):02d}:{match.group(2)}'
    node = soup.select_one('.mec-single-event-time .mec-events-abbr')
    match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', clean_text(node))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_location(description, category_ids):
    for pattern, venue, city in VENUE_PATTERNS:
        if re.search(pattern, description, re.I):
            return venue, city
    for category_id in category_ids:
        venue = CATEGORY_VENUES.get(category_id)
        if venue:
            return venue, 'Parma'
    return None


def parse_event(item, soup):
    schema = event_schema(soup)
    if not schema:
        return None
    event_date = str(schema.get('startDate', ''))[:10]
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except ValueError:
        return None

    title = clean_text(BeautifulSoup(item['title']['rendered'], 'html.parser'))
    description_soup = BeautifulSoup(item['content']['rendered'], 'html.parser')
    description = clean_text(description_soup)
    location = parse_location(description, item.get('mec_category', []))
    if not title or not location:
        return None

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': item['link'],
        'time_from': parse_time(soup, description),
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SocietaConcertiParmaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='societaconcertiparma_com',
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
            events = get_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Società dei Concerti di Parma event index',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for item in events:
            url = item.get('link')
            if not url:
                continue
            try:
                soup = BeautifulSoup(get_response(session, url).content, 'html.parser')
                record = parse_event(item, soup)
                if record:
                    records.append(record)
            except (requests.RequestException, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Società dei Concerti di Parma event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    SocietaConcertiParmaComCrawler().run()


if __name__ == '__main__':
    main()
