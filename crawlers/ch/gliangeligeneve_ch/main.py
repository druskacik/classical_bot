import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://gliangeligeneve.ch/'
SOURCE = 'Gli Angeli Genève'
AGENDA_URL = urljoin(SOURCE_URL, 'pages/agenda')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.7',
}

COUNTRY_MARKERS = {
    'B': 'BE', 'BE': 'BE', 'CH': 'CH', 'CR': 'HR', 'CZ': 'CZ', 'D': 'DE',
    'F': 'FR', 'HU': 'HU', 'NL': 'NL', 'PL': 'PL',
}

# Older seasons often omit the country even though the city is unambiguous.
CITY_COUNTRIES = {
    'amsterdam': 'NL', 'arnstadt': 'DE', 'basel': 'CH', 'bruges': 'BE',
    'bruxelles': 'BE', 'budapest': 'HU', 'bulle': 'CH', 'copenhagen': 'DK',
    'dole': 'FR', 'eisenach': 'DE', 'fribourg': 'CH', 'gdańsk': 'PL',
    'geneva': 'CH', 'genève': 'CH', 'gent': 'BE', 'hannover': 'DE',
    'hermance': 'CH', 'köln': 'DE', 'la tour-de-peilz': 'CH',
    'lausanne': 'CH', 'le sentier': 'CH', 'lutry': 'CH',
    'marcq-en-barœul': 'FR', 'monthey': 'CH', 'nijmegen': 'NL',
    'perros-guirec': 'FR', 'prague': 'CZ', 'rovigno': 'HR', 'rovinj': 'HR',
    'rotterdam': 'NL', 'saessolsheim': 'FR', 'saint-maurice': 'CH',
    'saintes': 'FR', 'sion': 'CH', 'st maurice': 'CH', 'tampere': 'FI',
    'utrecht': 'NL', 'vault-de-lugny': 'FR', 'vézelay': 'FR',
    'zutphen': 'NL',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value):
    match = re.search(r'\b(\d{2}/\d{2}/\d{2})\b', value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d/%m/%y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*[Hh:]\s*([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def split_location(node):
    if node is None:
        return '', ''
    parts = [clean_text(part) for part in node.stripped_strings if clean_text(part)]
    if len(parts) >= 2:
        return parts[0], ' '.join(parts[1:])
    return (parts[0], '') if parts else ('', '')


def detail_location(soup):
    heading = soup.select_one('h4')
    text = clean_text(heading)
    if ' – ' not in text:
        return '', ''
    return tuple(part.strip() for part in text.split(' – ', 1))


def normalize_city(raw_city, raw_venue):
    combined = f'{raw_city} {raw_venue}'
    if re.search(r'Rovinj|Rovigno', combined, re.IGNORECASE):
        return 'Rovinj'
    city = re.sub(r'\s*\([^)]*\)\s*', ' ', raw_city).strip()
    if city.lower().startswith('bachwochen ansbach'):
        return 'Ansbach'
    if city.lower().startswith('concerts de romainmôtier'):
        return 'Romainmôtier'
    return city


def infer_country(raw_city, raw_venue, city):
    for marker in re.findall(r'\(([A-Z]{1,2})\)', f'{raw_city} {raw_venue}'):
        if marker in COUNTRY_MARKERS:
            return COUNTRY_MARKERS[marker]
    return CITY_COUNTRIES.get(city.casefold())


def normalize_venue(value, city):
    venue = re.split(
        r'\s+[–-]\s+(?:Il reste|Billets?|Tickets?)\b', value, maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    venue = re.sub(r'\s*\((?:CH|F|D|NL|BE?|CZ|CR|HU|PL)\)\s*$', '', venue).strip()
    if not venue or venue.casefold() == city.casefold():
        return ''
    return venue


def parse_detail(html):
    soup = BeautifulSoup(html, 'html.parser')
    city, venue = detail_location(soup)
    content = soup.select_one('main') or soup.select_one('body')
    paragraphs = []
    for paragraph in content.find_all('p') if content else []:
        text = clean_text(paragraph)
        if text and not re.search(r'Questions\?|newsletter|site\.You can call', text, re.I):
            paragraphs.append(text)
    return city, venue, '\n\n'.join(dict.fromkeys(paragraphs)) or None


class GliAngeliGeneveCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gliangeligeneve_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def _get(self, session, url, params=None):
        response = session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return response.text

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            current_html = self._get(session, AGENDA_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Gli Angeli agenda', event='crawler_fetch_failed',
                level='error', url=AGENDA_URL, error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        current_soup = BeautifulSoup(current_html, 'html.parser')
        seasons = [
            option.get('value') for option in current_soup.select('select[name="season"] option[value]')
            if option.get('value')
        ]
        pages = [current_html]
        for season in seasons:
            try:
                pages.append(self._get(session, AGENDA_URL, {'season': season}))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Gli Angeli archive season', event='crawler_fetch_failed',
                    level='warning', url=AGENDA_URL, season=season,
                    error_type=type(error).__name__, error_message=str(error),
                )

        candidates = []
        for html in pages:
            soup = BeautifulSoup(html, 'html.parser')
            for card in soup.select('article.space-y-6 > div'):
                link = card.select_one('a[href^="/concerts/"]')
                title_node = link.select_one('h1') if link else None
                date_node = card.select_one('.uppercase')
                location_node = card.find('p')
                title = clean_text(title_node)
                event_date = parse_date(clean_text(date_node))
                url = urljoin(SOURCE_URL, link['href']) if link else ''
                if title and event_date and url:
                    raw_city, raw_venue = split_location(location_node)
                    candidates.append({
                        'title': title, 'date': event_date, 'url': url,
                        'time_from': parse_time(clean_text(card)),
                        'raw_city': raw_city, 'raw_venue': raw_venue,
                    })

        detail_data = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._get, session, url): url for url in {c['url'] for c in candidates}}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_data[url] = parse_detail(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Gli Angeli concert detail', event='crawler_fetch_failed',
                        level='warning', url=url, error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for candidate in candidates:
            detail_city, detail_venue, description = detail_data.get(candidate['url'], ('', '', None))
            raw_city = candidate['raw_city'] or detail_city
            raw_venue = candidate['raw_venue'] or detail_venue
            city = normalize_city(raw_city, raw_venue)
            venue = normalize_venue(raw_venue, city)
            country_code = infer_country(raw_city, raw_venue, city)
            if not all((city, venue, country_code)):
                log_message(
                    'Skipping concert with incomplete location', event='crawler_record_skipped',
                    level='warning', url=candidate['url'], city=city or None,
                    venue=venue or None,
                )
                continue
            records.append({
                'title': candidate['title'], 'date': candidate['date'],
                'url': candidate['url'], 'time_from': candidate['time_from'],
                'venue': venue, 'city': city, 'country_code': country_code,
                'description': description, 'source_url': SOURCE_URL, 'source': SOURCE,
            })
        return records


def main():
    GliAngeliGeneveCrawler().run()


if __name__ == '__main__':
    main()
