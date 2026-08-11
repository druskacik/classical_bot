import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cercledelharmonie.com/'
SOURCE = "Le Cercle de l'Harmonie"
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/concerts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
}

COUNTRIES = {
    'allemagne': 'DE', 'espagne': 'ES', 'italie': 'IT',
    'norvège': 'NO', 'norvege': 'NO',
}

# A few foreign listings provide only a venue or country rather than a postal city.
VENUE_GEOGRAPHY = {
    'audi sommerkonzerte ingolstad': ('Ingolstadt', 'DE'),
    'auditorio oviedo': ('Oviedo', 'ES'),
    'festival de ravello': ('Ravello', 'IT'),
    'kölner philharmonie': ('Cologne', 'DE'),
    'kurrsal auditorium': ('San Sebastián', 'ES'),
    'musikfest bremen': ('Bremen', 'DE'),
    'musikfest stuttgart': ('Stuttgart', 'DE'),
    'oslo international church music festival': ('Oslo', 'NO'),
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw
    )
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def event_index(session):
    events = []
    page = 1
    while True:
        batch = get_json(session, EVENTS_API, {'per_page': 100, 'page': page})
        events.extend(batch)
        if len(batch) < 100:
            return events
        page += 1


def parse_performance(value):
    text = clean_text(value).casefold()
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})\b', text
    )
    if not match:
        return None, []
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, []

    tail = text[match.end():]
    times = []
    for hour, minute in re.findall(r'(?<!\d)(\d{1,2})(?:h|:)(\d{2})?', tail):
        hour_number = int(hour)
        minute_number = int(minute or 0)
        if hour_number <= 23 and minute_number <= 59:
            formatted = f'{hour_number:02d}:{minute_number:02d}'
            if formatted not in times:
                times.append(formatted)
    return event_date, times or [None]


def resolve_geography(venue, address):
    venue_key = venue.casefold()
    if venue_key in VENUE_GEOGRAPHY:
        return VENUE_GEOGRAPHY[venue_key]

    text = clean_text(address).replace('\n', ' ')
    country_code = 'FR'
    folded = text.casefold()
    for country_name, code in COUNTRIES.items():
        if country_name in folded:
            country_code = code
            break

    # French and German/Spanish addresses on this site put the locality after
    # a five-digit postal code. Take the final such locality to avoid street text.
    postal_matches = list(re.finditer(r'\b\d{5}\s+(.+?)(?=\s+\d{5}\b|$)', text))
    if postal_matches:
        city = postal_matches[-1].group(1)
        city = re.sub(r'\s*\([^)]*\)\s*$', '', city).strip(' ,-')
        if city:
            return city, country_code

    city = re.sub(r'\s*\([^)]*\)\s*$', '', text).strip(' ,-')
    if city and city.casefold() not in COUNTRIES and not re.search(r'\d', city):
        return city, country_code

    # Some venue labels include a locality after a comma or en dash.
    parts = re.split(r'\s+[–—]\s+|,\s*', venue)
    if len(parts) > 1 and parts[-1].strip():
        return parts[-1].strip(), country_code
    return None, None


def page_description(soup):
    parts = []
    for selector in ('.content.description', '.programmes-content'):
        for node in soup.select(selector):
            text = clean_text(node)
            if text and text.casefold() != 'à propos' and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def detail_records(event):
    url = clean_text(event.get('link'))
    title = clean_text((event.get('title') or {}).get('rendered'))
    if not url or not title:
        return []
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    description = page_description(soup)
    records = []
    for calendar in soup.select('.calendrier'):
        date_node = calendar.select_one('.date span')
        venue_node = calendar.select_one('.lieu span')
        address_node = calendar.select_one('.adresse span')
        event_date, times = parse_performance(date_node)
        venue = clean_text(venue_node)
        city, country_code = resolve_geography(venue, clean_text(address_node))
        if not all((event_date, venue, city, country_code)):
            continue
        for time_from in times:
            records.append({
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
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = event_index(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_records, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Cercle de l\'Harmonie concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda row: (
        row['date'], row['time_from'] or '', row['title'], row['venue']
    ))


class CercleDeLHarmonieComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cercledelharmonie_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    CercleDeLHarmonieComCrawler().run()


if __name__ == '__main__':
    main()
