import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatromarrucino.eu/'
SOURCE = 'Teatro Marrucino'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
DEFAULT_VENUE = 'Teatro Marrucino'
DEFAULT_CITY = 'Chieti'

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4, 'maggio': 5,
    'giugno': 6, 'luglio': 7, 'agosto': 8, 'settembre': 9, 'ottobre': 10,
    'novembre': 11, 'dicembre': 12,
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    # Older event descriptions contain visible WordPress shortcode wrappers.
    text = re.sub(r'\[/?[A-Za-z_][^\]]*\]', '\n', text)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def event_location(event):
    venue = event.get('venue')
    if not venue:
        return DEFAULT_VENUE, DEFAULT_CITY, 'IT'
    if isinstance(venue, list):
        venue = venue[0] if venue else None
    if not isinstance(venue, dict):
        return None

    venue_name = clean_text(venue.get('venue'))
    city = clean_text(venue.get('city'))
    country = clean_text(venue.get('country')).lower()
    if not venue_name or not city:
        return None
    if not country or country in {'italia', 'italy'}:
        country_code = 'IT'
    else:
        # The API supplies country names rather than ISO codes. Do not invent a
        # code for an explicit touring venue that cannot be resolved safely.
        return None
    return venue_name, city, country_code


def event_record(event):
    title = clean_text(event.get('title'))
    url = str(event.get('url') or '').strip()
    start = str(event.get('start_date') or '')
    event_date = valid_date(start[:10])
    location = event_location(event)
    if not title or not url or not event_date or not location:
        return None

    description = clean_text(event.get('description'))
    all_day = bool(event.get('all_day'))
    time_from = None
    if not all_day and re.fullmatch(r'\d{2}:\d{2}', start[11:16]):
        time_from = start[11:16]
    elif description:
        # Some current records are incorrectly marked all-day although their
        # first-party description contains an exact advertised start time.
        match = re.search(r'\bore\s+(\d{1,2})[.:](\d{2})\b', description, re.I)
        if match:
            time_from = f'{int(match.group(1)):02d}:{match.group(2)}'

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def advertised_occurrences(record):
    """Return additional explicitly advertised performances on one event page."""
    description = record.get('description') or ''
    # Date listings are placed near the heading. Requiring a weekday, year and
    # time avoids treating biographical or repertoire dates as performances.
    intro = description[:1200]
    pattern = re.compile(
        r'\b(?:luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica)'
        r'\s+(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(20\d{2})'
        r'\s+ore\s+(\d{1,2})[.:](\d{2})\b',
        re.I,
    )
    occurrences = []
    for match in pattern.finditer(intro):
        try:
            event_date = date(
                int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1))
            ).isoformat()
        except ValueError:
            continue
        occurrence = record.copy()
        occurrence['date'] = event_date
        occurrence['time_from'] = f'{int(match.group(4)):02d}:{match.group(5)}'
        occurrences.append(occurrence)
    return occurrences or [record]


def fetch_events(session):
    page = 1
    events = []
    while True:
        params = {
            'start_date': '1900-01-01',
            'end_date': '2100-12-31',
            'per_page': 50,
            'page': page,
        }
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        batch = payload.get('events') or []
        events.extend(batch)
        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not batch:
            break
        page += 1
    return events


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)
    records = []
    for event in events:
        record = event_record(event)
        if record:
            records.extend(advertised_occurrences(record))
        else:
            log_message(
                'Skipped Teatro Marrucino event with incomplete required fields',
                event='crawler_item_skipped',
                level='warning',
                url=str(event.get('url') or ''),
            )

    # The site contains duplicated posts from a historical migration. Their
    # URLs differ, but their occurrence details are identical.
    unique = {}
    for record in records:
        key = (
            re.sub(r'\W+', '', record['title'].casefold()),
            record['date'],
            record['time_from'],
            record['venue'].casefold(),
        )
        unique.setdefault(key, record)
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class TeatroMarrucinoEuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatromarrucino_eu',
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
        return get_concerts()


def main():
    TeatroMarrucinoEuCrawler().run()


if __name__ == '__main__':
    main()
