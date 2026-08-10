import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ibermusica.es/es'
PROGRAM_URL = f'{SOURCE_URL}/programacion'
SOURCE = 'Ibermúsica'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}
MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'septiembre': 9,
    'setiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def inline_text(value):
    return re.sub(r'\s+', ' ', clean_text(value)).strip()


def normalize_venue(value):
    venue = inline_text(value)
    known_venues = {
        'auditorio nacional de música de madrid': 'Auditorio Nacional de Música de Madrid',
        'auditorio del centro de cultura contemporánea condeduque': (
            'Auditorio del Centro de Cultura Contemporánea CondeDuque'
        ),
    }
    return known_venues.get(venue.casefold(), venue)


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})'
        r'(?:\s*-\s*(\d{1,2}):(\d{2})\s*h\.?)?',
        clean_text(value).casefold(),
    )
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(4):
        hour, minute = int(match.group(4)), int(match.group(5))
        if hour < 24 and minute < 60:
            event_time = f'{hour:02d}:{minute:02d}'
    return event_date, event_time


def season_ids(session):
    soup = BeautifulSoup(get_response(session, PROGRAM_URL).text, 'html.parser')
    ids = []
    for option in soup.select('select[name="t"] option[value]'):
        value = option.get('value', '')
        if value.isdigit() and value != '0' and value not in ids:
            ids.append(value)
    return ids


def listing_items(session, season_id):
    url = f'{PROGRAM_URL}/{season_id}/0/1'
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    items = []
    for node in soup.select('.post-item-wrap'):
        link = node.select_one('h2 a[href*="/concierto/"]')
        date_node = node.select_one('.post-meta-date')
        venue_node = node.select_one('.post-meta-place')
        if not link or not date_node or not venue_node:
            continue
        event_date, event_time = parse_datetime(date_node)
        title = inline_text(link)
        event_url = urljoin(url, link.get('href', ''))
        venue = normalize_venue(venue_node)
        if not title or not event_date or not event_url or not venue:
            log_message(
                'Skipping event with incomplete listing fields',
                event='crawler_item_skipped',
                level='warning',
                url=event_url or url,
                missing_title=not bool(title),
                missing_date=not bool(event_date),
                missing_venue=not bool(venue),
            )
            continue
        items.append(
            {
                'title': title,
                'date': event_date,
                'url': event_url,
                'time_from': event_time,
                'venue': venue,
                'city': 'Madrid',
            }
        )
    return items


def add_description(session, item):
    soup = BeautifulSoup(get_response(session, item['url']).text, 'html.parser')
    program = soup.select_one('.concert-programa .text-content')
    return {**item, 'description': clean_text(program) or None}


class IbermusicaEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ibermusica_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        dedupe_subset=['url', 'date'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items_by_key = {}
        for season_id in season_ids(session):
            try:
                for item in listing_items(session, season_id):
                    items_by_key[(item['url'], item['date'])] = item
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape season listing',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{PROGRAM_URL}/{season_id}/0/1',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(add_description, session, item): item
                for item in items_by_key.values()
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    records.append(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=item['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    records.append({**item, 'description': None})
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    IbermusicaEsCrawler().run()


if __name__ == '__main__':
    main()
