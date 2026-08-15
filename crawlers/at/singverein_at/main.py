import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.singverein.at/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'archiv')
PROGRAMME_URL = urljoin(SOURCE_URL, 'konzerte/neu')
SOURCE = 'Wiener Singverein'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-AT,de;q=0.9,en;q=0.7',
}

# The archive normally prints only a hall, not its municipality. These are
# stable, unambiguous venue names used by the Singverein throughout its archive.
VENUES = {
    'musikverein': ('Wien', 'AT'),
    'konzerthaus': ('Wien', 'AT'),
    'stephansdom': ('Wien', 'AT'),
    'votivkirche': ('Wien', 'AT'),
    'hofburgkapelle': ('Wien', 'AT'),
    'alte oper': ('Frankfurt am Main', 'DE'),
    'elbphilharmonie': ('Hamburg', 'DE'),
    'kölner philharmonie': ('Köln', 'DE'),
    'philharmonie de paris': ('Paris', 'FR'),
    'concertgebouw': ('Amsterdam', 'NL'),
    'royal albert hall': ('London', 'GB'),
    'barbican': ('London', 'GB'),
    'national concert hall, taipeh': ('Taipei', 'TW'),
    'suntory hall': ('Tokyo', 'JP'),
    'carnegie hall': ('New York', 'US'),
    'basilika ottobeuren': ('Ottobeuren', 'DE'),
    'stiftskirche lilienfeld': ('Lilienfeld', 'AT'),
    'festspielhaus st. pölten': ('St. Pölten', 'AT'),
    'brucknerhaus': ('Linz', 'AT'),
    'schloß esterhazy': ('Eisenstadt', 'AT'),
    'schloss esterhazy': ('Eisenstadt', 'AT'),
    'grafenegg': ('Grafenegg', 'AT'),
}

CITY_COUNTRIES = {
    'Wien': 'AT', 'Salzburg': 'AT', 'Linz': 'AT', 'Graz': 'AT',
    'Eisenstadt': 'AT', 'St. Pölten': 'AT', 'Krems': 'AT',
    'Klagenfurt': 'AT', 'Bregenz': 'AT', 'Innsbruck': 'AT',
    'Berlin': 'DE', 'Hamburg': 'DE', 'Köln': 'DE', 'Cologne': 'DE',
    'Frankfurt': 'DE', 'München': 'DE', 'Munich': 'DE', 'Dresden': 'DE',
    'Leipzig': 'DE', 'Düsseldorf': 'DE', 'Bonn': 'DE',
    'Paris': 'FR', 'London': 'GB', 'Amsterdam': 'NL', 'Brüssel': 'BE',
    'Brussels': 'BE', 'Prag': 'CZ', 'Prague': 'CZ', 'Budapest': 'HU',
    'Rom': 'IT', 'Rome': 'IT', 'Mailand': 'IT', 'Milan': 'IT',
    'Zürich': 'CH', 'Zurich': 'CH', 'Taipeh': 'TW', 'Taipei': 'TW',
    'Tokyo': 'JP', 'New York': 'US', 'Moskau': 'RU', 'Moscow': 'RU',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def make_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update(HEADERS)
    return session


def resolve_location(raw_location):
    location = clean_text(raw_location).strip(' ,-')
    if not location:
        return None

    folded = location.casefold()
    for name, (city, country_code) in VENUES.items():
        if name in folded:
            venue = re.sub(
                rf'(?:,\s*)?\b{re.escape(city)}\b(?:\s*,)?',
                '',
                location,
                flags=re.I,
            ).strip(' ,-') or location
            # A bare municipality on this venue-specific calendar refers to
            # Schloss Grafenegg, rather than being used as a venue placeholder.
            if folded == 'grafenegg':
                venue = 'Schloss Grafenegg'
            return venue, city, country_code

    # Common archive notation is either "venue, city" or "city / venue".
    if '/' in location:
        city_part, venue = [part.strip() for part in location.split('/', 1)]
        for city, country_code in CITY_COUNTRIES.items():
            if city.casefold() in city_part.casefold() and venue:
                return venue, city, country_code

    for city, country_code in CITY_COUNTRIES.items():
        if re.search(rf'(?<!\w){re.escape(city)}(?!\w)', location, re.I):
            venue = re.sub(rf'(?:,\s*)?\b{re.escape(city)}\b', '', location, flags=re.I).strip(' ,-')
            if venue and venue.casefold() != city.casefold():
                return venue, city, country_code
    return None


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_programme_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for programme_index, wrapper in enumerate(soup.select('#konzerte .programm_wrapper'), start=1):
        programme = wrapper.select_one('.programm')
        if not programme:
            continue

        works = [clean_text(item) for item in programme.select('.werk_wrapper .werk')]
        artists = [clean_text(item) for item in programme.select('.kuenstler_wrapper .kuenstler')]
        direct_comment = programme.find('div', class_='kommentar', recursive=False)
        comment = clean_text(direct_comment)
        title = comment or (works[0] if works else '')
        if not title:
            continue

        description_parts = []
        if comment:
            description_parts.append(comment)
        if works:
            description_parts.append('Programm:\n' + '\n'.join(works))
        if artists:
            description_parts.append('Mitwirkende:\n' + '\n'.join(artists))
        description = '\n\n'.join(description_parts) or None

        for occurrence_index, occurrence in enumerate(wrapper.select('.ort_wrapper'), start=1):
            event_date = parse_date(occurrence.select_one('time'))
            resolved = resolve_location(occurrence.select_one('.ort'))
            if not event_date or not resolved:
                continue
            venue, city, country_code = resolved
            records.append({
                'title': title,
                'date': event_date,
                'url': f'{canonical_url(page_url)}#concert-{programme_index}-{occurrence_index}',
                'time_from': None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class SingvereinCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='singverein_at',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AT',
        upload_target='classical',
        dedupe_subset=['date', 'venue', 'title'],
    )

    def scrape(self):
        session = make_session()
        response = session.get(ARCHIVE_URL, timeout=45)
        response.raise_for_status()
        archive_soup = BeautifulSoup(response.text, 'html.parser')
        season_urls = sorted({
            canonical_url(urljoin(ARCHIVE_URL, link['href']))
            for link in archive_soup.select('main a[href*="/archiv/"][href$="saison"], main a[href*="/archiv/"][href*="/saison#"]')
        })

        # The separately maintained current-programme page includes announced
        # dates beyond the latest archive season, so it must be read as well.
        programme_response = session.get(PROGRAMME_URL, timeout=45)
        programme_response.raise_for_status()
        records = parse_programme_page(programme_response.text, PROGRAMME_URL)
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in season_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    season_response = future.result()
                    season_response.raise_for_status()
                    records.extend(parse_programme_page(season_response.text, url))
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Wiener Singverein season',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records.sort(key=lambda item: (item['date'], item['venue'], item['title']))
        return records


def main():
    SingvereinCrawler().run()


if __name__ == '__main__':
    main()
