import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filharmonija.si/'
SOURCE = 'Slovenska filharmonija'
PROGRAM_URL = urljoin(SOURCE_URL, 'koncerti/program/')
ARCHIVE_URL = f'{PROGRAM_URL}?archive=true'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sl-SI,sl;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'marec': 3,
    'april': 4,
    'maj': 5,
    'junij': 6,
    'julij': 7,
    'avgust': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'december': 12,
}

# Detail pages usually name a hall but omit its city. These are recurring,
# unambiguous venues in the Philharmonic calendar.
VENUE_CITIES = {
    'slovenska filharmonija': ('Ljubljana', 'SI'),
    'dvorana marjana kozine': ('Ljubljana', 'SI'),
    'dvorana slavka osterca': ('Ljubljana', 'SI'),
    'cankarjev dom': ('Ljubljana', 'SI'),
    'gallusova dvorana': ('Ljubljana', 'SI'),
    'križanke': ('Ljubljana', 'SI'),
    'ljubljanska tržnica': ('Ljubljana', 'SI'),
    'grand hotel union': ('Ljubljana', 'SI'),
    'dvorana union': ('Ljubljana', 'SI'),
    'živalski vrt ljubljana': ('Ljubljana', 'SI'),
    'stolnica sv. nikolaja': ('Ljubljana', 'SI'),
    'koncertna dvorana lotte': ('Seoul', 'KR'),
    'lotte concert hall': ('Seoul', 'KR'),
    'tokijsko metropolitansko gledališče': ('Tokyo', 'JP'),
    'tokyo metropolitan theatre': ('Tokyo', 'JP'),
}

CITY_COUNTRIES = {
    'Ljubljana': 'SI',
    'Maribor': 'SI',
    'Celje': 'SI',
    'Koper': 'SI',
    'Novo mesto': 'SI',
    'Nova Gorica': 'SI',
    'Velenje': 'SI',
    'Ptuj': 'SI',
    'Kranj': 'SI',
    'Seoul': 'KR',
    'Tokyo': 'JP',
    'Dunaj': 'AT',
    'Vienna': 'AT',
    'Gradec': 'AT',
    'Graz': 'AT',
    'Trst': 'IT',
    'Trieste': 'IT',
    'Zagreb': 'HR',
    'Budapest': 'HU',
}

LOCATION_STOP = re.compile(
    r'^(?:trajanje|v organizaciji|vstop|cena|prodaja|predkoncertni|koncertni list|'
    r'razprodano|odpovedano|festival ljubljana|rezervacij|karte|brezplač)',
    re.IGNORECASE,
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(
        r'\b(\d{1,2})\.\s*([a-zčšž]+)\s+(20\d{2})\b',
        value.casefold(),
    )
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\b', value)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def extract_venue(sidebar):
    location = sidebar.select_one('.editor-text') if sidebar else None
    lines = clean_text(location).splitlines()
    kept = []
    for line in lines:
        if LOCATION_STOP.search(line):
            break
        kept.append(line.strip(' ,'))
    # The first one or two lines are the hall and venue complex. Subsequent
    # lines are notices (tickets, organisers, duration) with inconsistent
    # wording and must not leak into the venue field.
    return ', '.join(line for line in kept[:2] if line).strip(' ,')


def parse_location(venue):
    if not venue:
        return None
    lower = venue.casefold()
    for token, (city, country_code) in VENUE_CITIES.items():
        if token in lower:
            return venue, city, country_code
    for city, country_code in CITY_COUNTRIES.items():
        if city.casefold() in lower:
            if venue.casefold().strip(' ,') == city.casefold():
                return None
            return venue, city, country_code
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    page = soup.select_one('.sc-single-page')
    if page is None:
        return []

    title_node = page.select_one('.single-page__title h1 span:first-child')
    title = clean_text(title_node)
    sidebar = page.select_one('.single-page__sidebar')
    location = parse_location(extract_venue(sidebar))
    if not title or sidebar is None or location is None:
        return []

    description_parts = [
        clean_text(page.select_one('.single-page__program')),
        clean_text(page.select_one('.single-page__body')),
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None
    venue, city, country_code = location
    records = []
    for occurrence in sidebar.select('.single-page__other-dates-item'):
        occurrence_text = clean_text(occurrence)
        event_date = parse_date(occurrence_text)
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(occurrence_text),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def listing_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for link in soup.select('a[href*="/koncert/"]'):
        url = urljoin(SOURCE_URL, link.get('href', '')).split('?', 1)[0]
        if url.startswith(urljoin(SOURCE_URL, 'koncert/')):
            urls.add(url)
    return urls


class FilharmonijaSiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonija_si',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SI',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def _fetch(self, url, timeout=60):
        log_message('Fetching Philharmonic URL', event='crawler_url_fetch', url=url)
        response = requests.get(url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        return response.content

    def _fetch_detail(self, url):
        try:
            return parse_detail(self._fetch(url, timeout=45), url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Philharmonic concert detail',
                event='crawler_detail_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return []

    def scrape(self):
        urls = set()
        for listing_url in (PROGRAM_URL, ARCHIVE_URL):
            urls.update(listing_urls(self._fetch(listing_url)))

        records = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {executor.submit(self._fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                records.extend(future.result())

        log_message(
            'Parsed Philharmonic calendar',
            event='crawler_parse_completed',
            url=PROGRAM_URL,
            record_count=len(records),
        )
        return records


def main():
    FilharmonijaSiCrawler().run()


if __name__ == '__main__':
    main()
