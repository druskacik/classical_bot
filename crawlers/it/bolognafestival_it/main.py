import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bolognafestival.it/it/'
API_URL = 'https://www.bolognafestival.it/wp-json/wp/v2'
SOURCE = 'Bologna Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup(['script', 'style', 'figure', 'iframe']):
        node.decompose()
    text = html.unescape(soup.get_text('\n', strip=True))
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_collection(session, endpoint):
    records = []
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{endpoint}',
            params={'per_page': 100, 'page': page, 'wpml_language': 'it'},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError(f'Unexpected API response for {endpoint}')
        records.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return records
        page += 1


def parse_date(value):
    try:
        return datetime.strptime(str(value), '%Y%m%d').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})(?::\d{2})?', str(value or '').strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def text_occurrences(acf):
    occurrences = []
    for line in str(acf.get('text_data_evento') or '').splitlines():
        match = re.search(
            r'\b(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|'
            r'luglio|agosto|settembre|ottobre|novembre|dicembre)\s+(\d{4})'
            r'(?:\s+ore\s+(\d{1,2})(?:(?::|\.)(\d{2}))?)?',
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        months = {
            'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
            'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
            'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
        }
        try:
            event_date = datetime(
                int(match.group(3)), months[match.group(2).casefold()], int(match.group(1))
            ).date().isoformat()
        except ValueError:
            continue
        event_time = None
        if match.group(4):
            event_time = parse_time(f'{match.group(4)}:{match.group(5) or "00"}')
        occurrences.append((event_date, event_time))
    return occurrences


def occurrences(acf):
    result = []
    for date_entry in acf.get('date_singole') or []:
        event_date = parse_date(date_entry.get('data'))
        if not event_date:
            continue
        times = date_entry.get('orari_evento') or [{}]
        for time_entry in times:
            result.append((event_date, parse_time(time_entry.get('inizia_alle_event'))))
    if result:
        return result

    if acf.get('formato_data_evento') == 'text':
        result = text_occurrences(acf)
        if result:
            return result

    event_date = parse_date(acf.get('inizio_evento'))
    if not event_date:
        return []
    times = acf.get('orari_evento') or [{}]
    return [(event_date, parse_time(item.get('inizia_alle_event'))) for item in times]


def description(event):
    acf = event.get('acf') or {}
    parts = []
    heading = clean_text(acf.get('intestazione_artisti_alt_title'))
    if heading:
        parts.append(heading)
    artists = []
    for artist in acf.get('artisti_alt_title') or []:
        name = clean_text(artist.get('nome_artista'))
        role = clean_text(artist.get('testo_aggiuntivo'))
        if name:
            artists.append(' — '.join(part for part in (name, role) if part))
    if artists:
        parts.append('\n'.join(artists))
    repertoire = clean_text(acf.get('descrizione_artisti_alt_title'))
    if repertoire:
        parts.append(repertoire)
    body = clean_text((event.get('content') or {}).get('rendered'))
    if body:
        parts.append(body)
    return '\n\n'.join(dict.fromkeys(parts)) or None


def event_venue(event, locations):
    location_ids = event.get('location') or []
    for location_id in location_ids:
        if locations.get(location_id):
            return locations[location_id]
    acf_location = (event.get('acf') or {}).get('location_event') or {}
    custom = clean_text(acf_location.get('location_custom')).split('\n', 1)[0].strip()
    return custom or None


class BolognaFestivalItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bolognafestival_it',
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
            location_terms = api_collection(session, 'location')
            events = api_collection(session, 'evento')
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Bologna Festival API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        locations = {
            item.get('id'): clean_text(item.get('name'))
            for item in location_terms
            if item.get('id') and clean_text(item.get('name'))
        }
        records = []
        for event in events:
            url = event.get('link')
            title = clean_text((event.get('title') or {}).get('rendered'))
            venue = event_venue(event, locations)
            event_dates = occurrences(event.get('acf') or {})
            if not title or not url or not venue or not event_dates:
                log_message(
                    'Skipping incomplete Bologna Festival event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url or API_URL,
                    has_title=bool(title),
                    has_venue=bool(venue),
                    occurrence_count=len(event_dates),
                )
                continue
            event_description = description(event)
            for event_date, event_time in event_dates:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': event_time,
                    'venue': venue,
                    'city': 'Bologna',
                    'country_code': 'IT',
                    'description': event_description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    BolognaFestivalItCrawler().run()


if __name__ == '__main__':
    main()
