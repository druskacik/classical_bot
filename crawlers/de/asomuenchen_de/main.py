import csv
import html
import io
import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.asomuenchen.de/'
SOURCE = 'Akademisches Sinfonieorchester München'
EVENTS_URL = f'{SOURCE_URL}konzerte/liste/'
ARCHIVE_CSV_URL = (
    'https://docs.google.com/spreadsheets/d/'
    '1Y3PrP3mO48_4tO3xpFj0sqMRu7LOnA-I4Cf4mHWG9iQ/pub?output=csv'
)
ARCHIVE_URL = f'{SOURCE_URL}konzerte/'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

VENUE_LOCATIONS = (
    (('herkulessaal', 'herkulesssaal', 'herkulessal'), 'Herkulessaal der Residenz', 'München'),
    (('brunnenhof der residenz',), 'Brunnenhof der Residenz', 'München'),
    (('max-joseph-saal',), 'Max-Joseph-Saal der Residenz', 'München'),
    (('isarphilharmonie',), 'Isarphilharmonie', 'München'),
    (('veranstaltungsforum fürstenfeld',), 'Veranstaltungsforum Fürstenfeld', 'Fürstenfeldbruck'),
    (('klosterkirche fürstenfeld',), 'Klosterkirche Fürstenfeld', 'Fürstenfeldbruck'),
    (('stadthalle germering', 'stadhalle germering'), 'Stadthalle Germering', 'Germering'),
)


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw and '>' in raw
        else html.unescape(raw)
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def archive_location(headline):
    folded = clean_text(headline).casefold()
    for aliases, venue, city in VENUE_LOCATIONS:
        if any(alias in folded for alias in aliases):
            return venue, city
    return None, None


def parse_archive_datetime(value):
    raw = clean_text(value)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%m/%d/%Y %H:%M:%S')
    except ValueError:
        return None


def archive_records(csv_text):
    records = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        start = parse_archive_datetime(row.get('Start Date'))
        title = clean_text(row.get('Headline'))
        venue, city = archive_location(title)
        if not start or not title or not venue or not city:
            continue
        description = clean_text(row.get('Text')) or None
        # The public timeline has no per-slide URL. A dated fragment provides
        # a stable, unique source reference without pretending an image is the event page.
        url = f'{ARCHIVE_URL}#chronik-{start.date().isoformat()}'
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M') if start.time() != datetime.min.time() else None,
            'venue': venue,
            'city': city,
            'country_code': 'DE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def event_links(html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    return {
        link.get('href').rstrip('/')
        for link in soup.select('.tribe-events-calendar-list__event-title-link[href]')
    }


def event_schema(html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload.get('@graph', []) if isinstance(payload, dict) else []
        for node in nodes:
            if node.get('@type') == 'Event':
                return node
    return None


def detail_record(html_text, fallback_url):
    event = event_schema(html_text)
    if not event:
        return None
    title = clean_text(event.get('name'))
    url = clean_text(event.get('url')) or fallback_url
    start_raw = event.get('startDate')
    location = event.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    if not all((title, url, start_raw, venue, city)):
        return None
    try:
        start = datetime.fromisoformat(start_raw)
    except (TypeError, ValueError):
        return None

    soup = BeautifulSoup(html_text, 'html.parser')
    body = soup.select_one('.tribe-events-single-event-description')
    description = clean_text(str(body)) if body else clean_text(event.get('description'))
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url.rstrip('/'),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AsomuenchenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='asomuenchen_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = archive_records(get(session, ARCHIVE_CSV_URL).text)

        links = set()
        for params in (None, {'eventDisplay': 'past'}):
            links.update(event_links(get(session, EVENTS_URL, params=params).text))
        for url in sorted(links):
            try:
                record = detail_record(get(session, url).text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape ASO concert detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)

        # Prefer full WordPress detail records where the timeline overlaps it.
        unique = {}
        for record in records:
            key = (record['date'], record['time_from'], record['venue'])
            unique[key] = record
        return sorted(
            unique.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    AsomuenchenDeCrawler().run()


if __name__ == '__main__':
    main()
