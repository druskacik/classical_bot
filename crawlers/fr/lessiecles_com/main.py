import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lessiecles.com/'
SOURCE = 'Les Siècles'
SITEMAP_URL = f'{SOURCE_URL}mec-events-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5, 'juin': 6,
    'juillet': 7, 'aout': 8, 'septembre': 9, 'octobre': 10,
    'novembre': 11, 'decembre': 12,
    'jan': 1, 'fev': 2, 'avr': 4, 'juil': 7, 'aou': 8, 'sep': 9,
    'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

# Les Siècles is French but tours internationally. MEC stores the city at the
# start of its location label and does not populate the structured address.
FOREIGN_CITIES = {
    'amsterdam': 'NL', 'athenes': 'GR', 'barcelone': 'ES', 'berlin': 'DE',
    'bruxelles': 'BE', 'budapest': 'HU', 'cologne': 'DE', 'dortmund': 'DE',
    'dresde': 'DE', 'essen': 'DE', 'geneve': 'CH', 'hambourg': 'DE',
    'innsbruck': 'AT', 'londres': 'GB', 'lucerne': 'CH', 'madrid': 'ES',
    'milan': 'IT', 'montreux': 'CH', 'munich': 'DE', 'pekin': 'CN',
    'prague': 'CZ', 'salzbourg': 'AT', 'seoul': 'KR', 'tokyo': 'JP',
    'vienne': 'AT', 'wiesbaden': 'DE', 'zurich': 'CH',
}

COUNTRY_NAMES = {
    'allemagne': 'DE', 'autriche': 'AT', 'belgique': 'BE', 'chine': 'CN',
    'coree du sud': 'KR', 'espagne': 'ES', 'grece': 'GR', 'hongrie': 'HU',
    'italie': 'IT', 'japon': 'JP', 'pays-bas': 'NL', 'republique tcheque': 'CZ',
    'royaume-uni': 'GB', 'suisse': 'CH',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalize(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(character)
    )


def parse_french_date(value):
    match = re.search(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})', value)
    if not match:
        return None
    month = MONTHS.get(normalize(match.group(2)))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(value, address=''):
    parts = [part.strip(' -') for part in value.split(',', 1)]
    if len(parts) != 2 or not all(parts):
        return None
    city, venue = parts
    normalized_address = normalize(address)
    country_code = next(
        (code for name, code in COUNTRY_NAMES.items() if name in normalized_address),
        FOREIGN_CITIES.get(normalize(city), 'FR'),
    )
    return venue, city.title(), country_code


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.mec-single-title'))
    event_date = parse_french_date(clean_text(
        soup.select_one('.mec-single-event-date .mec-start-date-label')
    ))
    location_element = soup.select_one('.mec-single-event-location')
    location = parse_location(
        clean_text(location_element.select_one('.mec-meta-label')) if location_element else '',
        clean_text(location_element.select_one('.mec-address')) if location_element else '',
    )
    if not title or not event_date or not location:
        return None

    time_text = clean_text(soup.select_one('.mec-single-event-time abbr'))
    time_match = re.search(r'\b([01]?\d|2[0-3])\s*(?:h|:)\s*([0-5]\d)\b', time_text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    venue, city, country_code = location
    description = clean_text(soup.select_one('.mec-single-event-description')) or None

    return {
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
    }


class LessieclesComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lessiecles_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SITEMAP_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Les Siècles event sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        sitemap = BeautifulSoup(response.text, 'xml')
        event_urls = sorted({
            clean_text(location)
            for location in sitemap.select('url > loc')
            if clean_text(location).startswith(f'{SOURCE_URL}events/')
            and clean_text(location).rstrip('/') != f'{SOURCE_URL}events'
        })

        def fetch_event(url):
            try:
                # Use an independent connection per worker: requests.Session is
                # not guaranteed to be thread-safe.
                event_response = requests.get(url, headers=HEADERS, timeout=45)
                event_response.raise_for_status()
                return parse_event(event_response.text, event_response.url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Les Siècles event',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                return None

        records = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {executor.submit(fetch_event, url): url for url in event_urls}
            for future in as_completed(futures):
                try:
                    record = future.result()
                except Exception as error:
                    log_message(
                        'Failed to parse Les Siècles event',
                        event='crawler_parse_failed',
                        level='warning',
                        url=futures[future],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['url']))


def main():
    LessieclesComCrawler().run()


if __name__ == '__main__':
    main()
