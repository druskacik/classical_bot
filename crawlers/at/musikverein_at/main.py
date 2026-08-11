import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musikverein.at/'
SCHEDULE_URL = 'https://spielplan.musikverein.at/spielplan'
ARCHIVE_URL = 'https://spielplan.musikverein.at/archiv'
DETAIL_API = 'https://spielplan.musikverein.at/e/{event_id}.json'
SOURCE = 'Musikverein Wien'
CITY = 'Wien'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-AT,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, data=None):
    response = session.post(url, data=data, timeout=120) if data else session.get(url, timeout=120)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_ids(soup):
    ids = set()
    for link in soup.select('a[href*="/konzert/"][href*="id="]'):
        values = parse_qs(urlparse(link.get('href', '')).query).get('id') or []
        if values and re.fullmatch(r'[0-9a-fA-F]+', values[0]):
            ids.add(values[0].lower())
    return ids


def month_windows(start, end):
    cursor = start.replace(day=1)
    while cursor <= end:
        following = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(cursor, start), min(following - timedelta(days=1), end)
        cursor = following


def listing_ids(session):
    today = date.today()
    # Retain a complete previous/current Musikverein season. The archive is a
    # separate first-party search and supports bounded date ranges.
    season_start = date(today.year if today.month >= 9 else today.year - 1, 9, 1)
    ids = set()
    for start, end in month_windows(season_start, today - timedelta(days=1)):
        soup = get_soup(
            session,
            ARCHIVE_URL,
            {
                'date_start_from': start.isoformat(),
                'date_start_to': end.isoformat(),
                'submit': 'Suchen',
            },
        )
        ids.update(event_ids(soup))

    # The live schedule returns at most 500 occurrences and has no end-date
    # field. Advancing the start date beyond the last returned occurrence
    # preserves the unfiltered feed while avoiding that cap.
    cursor = today
    for _ in range(12):
        soup = get_soup(
            session,
            SCHEDULE_URL,
            {'date_start_from': cursor.isoformat(), 'submit': 'Suchen'},
        )
        page_ids = event_ids(soup)
        new_ids = page_ids - ids
        ids.update(page_ids)
        dates = []
        for node in soup.select('.day--heading'):
            match = re.search(r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{4})', node.get_text(' ', strip=True))
            if match:
                month_names = {
                    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4,
                    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8,
                    'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12,
                }
                month = month_names.get(match.group(2).lower())
                if month:
                    dates.append(date(int(match.group(3)), month, int(match.group(1))))
        if not page_ids or not dates or not new_ids:
            break
        next_cursor = max(dates) + timedelta(days=1)
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    return ids


def get_json(session, event_id):
    response = session.get(DETAIL_API.format(event_id=event_id), timeout=60)
    response.raise_for_status()
    return response.json()


def first_row(payload, key):
    rows = (payload.get(key) or {}).get('data') or []
    return rows[0] if rows else {}


def description_from(payload, booking):
    parts = []
    for key in (
        'comm_descr_short_1_ch_1_D',
        'comm_descr_long_1_HTML_ch_1_D',
        'comm_important_note_1_ch_0_html_D',
    ):
        value = clean_text(booking.get(key))
        if value and value not in parts:
            parts.append(value)

    cast = []
    for item in (payload.get('cast') or {}).get('data') or []:
        name = clean_text(item.get('name_D'))
        role = clean_text(item.get('profession_D') or item.get('role_D'))
        if name:
            cast.append(f'{name} — {role}' if role else name)
    if cast:
        parts.append('Interpret:innen\n' + '\n'.join(cast))

    works = []
    program = (payload.get('program') or {}).get('data') or []
    for item in sorted(program, key=lambda row: int(row.get('order') or 0)):
        composer = clean_text(item.get('composer_author'))
        work = clean_text(item.get('opus_titel_D'))
        if work and work != '***':
            works.append(f'{composer}: {work}' if composer and composer != '***' else work)
    if works:
        parts.append('Programm\n' + '\n'.join(works))
    return '\n\n'.join(parts) or None


def make_record(event_id, payload):
    booking = first_row(payload, 'booking')
    title_parts = [clean_text(booking.get(key)) for key in ('name_1_web_D', 'name_2_web_D', 'name_3_web_D')]
    title_parts = [part for part in title_parts if part]
    title = ' – '.join(dict.fromkeys(title_parts))
    start = booking.get('date_start') or ''
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2}', start)
    venue_name = clean_text(booking.get('venue_description'))
    room = clean_text(booking.get('room_description'))
    if not title or not match or not venue_name:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    # This calendar is for performances inside the Musikverein building.
    # Requiring its explicit API venue prevents a home-city default from being
    # applied to any future touring entry.
    if 'musikverein' not in venue_name.lower():
        return None
    venue = f'{room}, {venue_name}' if room and room.lower() != venue_name.lower() else venue_name
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, f'konzert/?id={event_id}'),
        'time_from': match.group(2),
        'venue': venue,
        'city': CITY,
        'country_code': 'AT',
        'description': description_from(payload, booking),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    ids = listing_ids(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_json, session, event_id): event_id for event_id in ids}
        for future in as_completed(futures):
            event_id = futures[future]
            try:
                record = make_record(event_id, future.result())
            except (requests.RequestException, ValueError, TypeError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=urljoin(SOURCE_URL, f'konzert/?id={event_id}'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']))


class MusikvereinAtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musikverein_at',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MusikvereinAtCrawler().run()


if __name__ == '__main__':
    main()
