import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ensemble-mosaik.de/'
SOURCE = 'ensemble mosaik'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'maerz': 3, 'april': 4,
    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'dezember': 12,
}

COUNTRY_MARKERS = {
    'austria': 'AT', 'österreich': 'AT', 'belgium': 'BE', 'belgien': 'BE',
    'canada': 'CA', 'china': 'CN', 'czech republic': 'CZ', 'tschechien': 'CZ',
    'denmark': 'DK', 'dänemark': 'DK', 'france': 'FR', 'frankreich': 'FR',
    'germany': 'DE', 'deutschland': 'DE', 'italy': 'IT', 'italien': 'IT',
    'japan': 'JP', 'netherlands': 'NL', 'niederlande': 'NL', 'norway': 'NO',
    'norwegen': 'NO', 'poland': 'PL', 'polen': 'PL', 'portugal': 'PT',
    'spain': 'ES', 'spanien': 'ES', 'sweden': 'SE', 'schweden': 'SE',
    'switzerland': 'CH', 'schweiz': 'CH', 'united kingdom': 'GB', 'uk': 'GB',
    'usa': 'US', 'united states': 'US',
}

# The calendar often gives only a city for touring engagements. These are
# deliberately limited to unambiguous cities observed in European concert data.
CITY_COUNTRIES = {
    'amsterdam': 'NL', 'basel': 'CH', 'berlin': 'DE', 'bern': 'CH',
    'birmingham': 'GB', 'bologna': 'IT', 'bonn': 'DE', 'brussels': 'BE',
    'brüssel': 'BE', 'cologne': 'DE', 'darmstadt': 'DE', 'dresden': 'DE',
    'düsseldorf': 'DE', 'essen': 'DE', 'frankfurt': 'DE', 'freiburg': 'DE',
    'geneva': 'CH', 'genf': 'CH', 'hamburg': 'DE', 'hannover': 'DE',
    'helsinki': 'FI', 'huddersfield': 'GB', 'köln': 'DE', 'leipzig': 'DE',
    'london': 'GB', 'lucerne': 'CH', 'luzern': 'CH', 'madrid': 'ES',
    'munich': 'DE', 'münchen': 'DE', 'paris': 'FR', 'prague': 'CZ',
    'prag': 'CZ', 'rom': 'IT', 'rome': 'IT', 'salzburg': 'AT',
    'stuttgart': 'DE', 'tokyo': 'JP', 'warschau': 'PL', 'warsaw': 'PL',
    'wien': 'AT', 'vienna': 'AT', 'zürich': 'CH', 'zurich': 'CH',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = re.search(
        r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s+(20\d{2})'
        r'(?:\s*/\s*(\d{1,2}):([0-5]\d))?',
        value,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    event_time = f'{int(match.group(4)):02d}:{match.group(5)}' if match.group(4) else None
    return event_date, event_time


def parse_location(element):
    if element is None:
        return None
    lines = [line.strip(' ,') for line in clean_text(element).splitlines() if line.strip(' ,')]
    if not lines:
        return None
    venue_node = element.select_one('b, strong')
    venue = clean_text(venue_node) if venue_node else lines[0]
    if not venue:
        return None

    full_text = ' '.join(lines).lower()
    country_code = next(
        (code for marker, code in COUNTRY_MARKERS.items() if re.search(rf'\b{re.escape(marker)}\b', full_text)),
        None,
    )

    candidates = list(reversed(lines[1:] if len(lines) > 1 else lines))
    city = None
    for line in candidates:
        simplified = re.sub(r'^\d{4,6}\s+', '', line).strip()
        simplified = re.sub(r'\s*,\s*(?:Germany|Deutschland|[A-Z]{2})$', '', simplified, flags=re.I)
        if simplified.lower() in COUNTRY_MARKERS or re.fullmatch(r'[A-Z]{2}', simplified):
            continue
        if re.search(r'\b(?:str\.?|straße|strasse|road|street|avenue|platz|weg)\b', simplified, re.I):
            continue
        city = simplified
        break
    if not city or city == venue:
        return None

    city_key = city.casefold()
    if country_code is None:
        country_code = CITY_COUNTRIES.get(city_key)
    if country_code is None and re.search(r'\b\d{5}\b', full_text):
        country_code = 'DE'
    if country_code is None:
        return None
    return venue, city, country_code


def parse_event_page(html, fallback):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.wp-block-post-title')) or clean_text(
        BeautifulSoup(fallback.get('title', ''), 'html.parser')
    )
    event_date, time_from = parse_date_time(clean_text(soup.select_one('.event-dates')))
    location = parse_location(soup.select_one('.event-location'))
    if not title or not event_date or not location:
        return None
    venue, city, country_code = location
    description = clean_text(soup.select_one('.entry-content')) or None
    return {
        'title': title,
        'date': event_date,
        'url': fallback['link'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class EnsembleMosaikDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensemble_mosaik_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def _event_index(self, session):
        events = []
        page = 1
        while True:
            response = session.get(
                API_URL,
                params={'per_page': 100, 'page': page, '_fields': 'link,title'},
                timeout=45,
            )
            response.raise_for_status()
            events.extend(response.json())
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                return events
            page += 1

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = self._event_index(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch ensemble mosaik event index',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        def fetch(event):
            response = session.get(event['link'], timeout=45)
            response.raise_for_status()
            return parse_event_page(response.text, {
                'link': event['link'],
                'title': event.get('title', {}).get('rendered', ''),
            })

        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch, event): event['link'] for event in events}
            for future in as_completed(futures):
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch ensemble mosaik event',
                        event='crawler_event_fetch_failed',
                        level='warning',
                        url=futures[future],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ))


def main():
    EnsembleMosaikDeCrawler().run()


if __name__ == '__main__':
    main()
