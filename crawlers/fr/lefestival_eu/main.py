import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lefestival.eu/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
SOURCE = 'Festival Radio France Occitanie Montpellier'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
    'decembre': 12,
}

DATE_RE = re.compile(
    r'(?i)\b(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+'
    r'(\d{1,2})\s+'
    r'(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)'
    r'\s+(\d{4})\b'
)
TIME_RE = re.compile(r'(?<!\d)([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)?(?!\d)', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_items(session, endpoint, fields=None):
    items = []
    page = 1
    while True:
        params = {'per_page': 100, 'page': page}
        if fields:
            params['_fields'] = fields
        response = session.get(f'{API_URL}/{endpoint}', params=params, timeout=60)
        response.raise_for_status()
        items.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return items
        page += 1


def taxonomy_map(session, endpoint):
    return {item['id']: clean_text(item['name']) for item in api_items(session, endpoint)}


def parse_occurrence(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    content = soup.select_one('#content')
    if not content:
        return None
    text = clean_text(content)
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None

    nearby = text[match.end():match.end() + 80]
    time_match = TIME_RE.search(nearby)
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2) or "00"}'
    return event_date, time_from, text or None


def city_from_page(description, cities, preferred_ids):
    header = description[:600]
    preferred = [cities[city_id] for city_id in preferred_ids if city_id in cities]
    candidates = preferred or list(cities.values())
    matches = []
    for candidate in candidates:
        city = re.sub(r'^À\s+', '', candidate, flags=re.I).strip()
        if len(city) >= 4 and re.search(rf'(?<!\w){re.escape(city)}(?!\w)', header, re.I):
            matches.append(city)
    if not matches:
        return None
    return max(matches, key=len).title()


def event_record(event, venues, cities):
    title = clean_text((event.get('title') or {}).get('rendered'))
    url = event.get('link') or ''
    venue_ids = event.get('salle') or []
    city_ids = event.get('ville_rep') or []
    venue = venues.get(venue_ids[0]) if len(venue_ids) == 1 else None
    city = cities.get(city_ids[0]) if len(city_ids) == 1 else None
    if city:
        city = re.sub(r'^À\s+', '', city, flags=re.I).strip().title()
    if not title or not url or not venue:
        return None

    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    occurrence = parse_occurrence(response.text)
    if not occurrence:
        return None
    event_date, time_from, description = occurrence
    city = city or city_from_page(description, cities, city_ids)
    if not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        events = api_items(
            session,
            'representation',
            'id,link,title,salle,serie,type_rpst,ville_rep',
        )
        venues = taxonomy_map(session, 'salle')
        cities = taxonomy_map(session, 'ville_rep')
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch Festival Radio France catalogue',
            event='crawler_fetch_failed',
            level='error',
            url=f'{API_URL}/representation',
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(event_record, event, venues, cities): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Festival Radio France event',
                    event='crawler_event_fetch_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class LefestivalEuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lefestival_eu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
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
    LefestivalEuCrawler().run()


if __name__ == '__main__':
    main()
