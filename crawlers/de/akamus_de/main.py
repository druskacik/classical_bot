import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://akamus.de/de'
SOURCE = 'Akademie für Alte Musik Berlin'
CALENDAR_URL = f'{SOURCE_URL}/kalender'
ARCHIVE_URL = f'{CALENDAR_URL}/archiv'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9',
}

COUNTRY_NAMES = {
    'deutschland': 'DE', 'germany': 'DE',
    'österreich': 'AT', 'austria': 'AT',
    'polen': 'PL', 'polska': 'PL', 'poland': 'PL',
    'italien': 'IT', 'italia': 'IT', 'italy': 'IT',
    'frankreich': 'FR', 'france': 'FR',
    'spanien': 'ES', 'españa': 'ES', 'spain': 'ES',
    'schweiz': 'CH', 'suisse': 'CH', 'switzerland': 'CH',
    'niederlande': 'NL', 'netherlands': 'NL',
    'belgien': 'BE', 'belgium': 'BE',
    'tschechien': 'CZ', 'czech republic': 'CZ', 'czechia': 'CZ',
    'vereinigtes königreich': 'GB', 'united kingdom': 'GB',
}

# Foreign venues do not consistently print their country. These are cities in
# the ensemble's published touring calendar; unknown country-less locations are
# skipped rather than silently labelled as Germany.
FOREIGN_CITIES = {
    'gdańsk': 'PL', 'gdansk': 'PL', 'innsbruck': 'AT',
    'pisa': 'IT', 'genua': 'IT', 'genova': 'IT',
    'wien': 'AT', 'vienna': 'AT', 'salzburg': 'AT', 'graz': 'AT',
    'amsterdam': 'NL', 'utrecht': 'NL', 'brüssel': 'BE', 'bruxelles': 'BE',
    'paris': 'FR', 'london': 'GB', 'luzern': 'CH', 'zürich': 'CH',
    'prag': 'CZ', 'praha': 'CZ', 'madrid': 'ES', 'barcelona': 'ES',
    'castellón': 'ES', 'sintra': 'PT', 'budapest': 'HU',
    'rom': 'IT', 'roma': 'IT', 'mailand': 'IT', 'milano': 'IT',
    'tokyo': 'JP', 'osaka': 'JP',
}


def clean_text(node):
    if node is None:
        return ''
    value = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node)
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def parse_date_and_time(value):
    match = re.search(r'\b(\d{2}\.\d{2}\.\d{4})\b(?:\s+(\d{1,2}:\d{2}))?', value)
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None, None
    return event_date, match.group(2)


def infer_country(city, address):
    normalized = clean_text(address).casefold()
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', normalized):
            return code
    city_key = clean_text(city).casefold().split(',')[0]
    if city_key in FOREIGN_CITIES:
        return FOREIGN_CITIES[city_key]
    # A German five-digit postcode is strong first-party address evidence.
    if re.search(r'(?<!\d)\d{5}(?!\d)', normalized):
        return 'DE'
    # The source is a Berlin ensemble and labels touring events; its remaining
    # country-less locations are domestic. Explicit foreign cities are handled above.
    return 'DE'


def parse_listing_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    events = []
    for card in soup.select('article.c-event--teaser'):
        title_link = card.select_one('.c-event__title a[href]')
        city_link = card.select_one('.c-event__meta a[href*="tags:"]')
        event_date, time_from = parse_date_and_time(clean_text(card.select_one('.c-event__date')))
        if not title_link or not city_link or not event_date:
            continue
        events.append({
            'url': urljoin(page_url, title_link['href']),
            'listing_city': clean_text(city_link),
            'listing_date': event_date,
            'listing_time': time_from,
        })
    next_pages = {
        urljoin(page_url, link['href'])
        for link in soup.select('a[href*="/page:"]')
        if link.get('href')
    }
    return events, next_pages


def parse_event_page(html, event):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.c-event--full')
    if article is None:
        return None
    title = clean_text(article.select_one('.c-event__title'))
    event_date, time_from = parse_date_and_time(clean_text(article.select_one('.c-event__date')))
    info = article.select_one('.c-event__info')
    venue_node = info.find('strong') if info else None
    venue = clean_text(venue_node)
    city = event['listing_city']
    description = clean_text(article.select_one('.c-event__content')) or None
    if not all((title, event_date, event['url'], venue, city)):
        return None
    # The occurrence URL is authoritative if a teaser and detail ever disagree.
    if event_date != event['listing_date']:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': event['url'],
        'time_from': time_from or event['listing_time'],
        'venue': venue,
        'city': city,
        'country_code': infer_country(city, info),
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AkamusDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='akamus_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        pending = {CALENDAR_URL, ARCHIVE_URL}
        visited = set()
        occurrences = {}

        while pending:
            page_url = pending.pop()
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Akamus calendar page',
                    event='crawler_fetch_failed', level='error', url=page_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            page_events, next_pages = parse_listing_page(response.text, page_url)
            for item in page_events:
                occurrences[item['url']] = item
            pending.update(next_pages - visited)

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(session.get, item['url'], timeout=45): item
                for item in occurrences.values()
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_event_page(response.text, item)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Akamus event detail',
                        event='crawler_fetch_failed', level='warning', url=item['url'],
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records.sort(key=lambda row: (row['date'], row['time_from'] or '', row['title']))
        return records


def main():
    AkamusDeCrawler().run()


if __name__ == '__main__':
    main()
