import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.allegro-vivo.at/de/'
SOURCE = 'Allegro Vivo'
LIST_URLS = (
    urljoin(SOURCE_URL, 'concerts/'),
    urljoin(SOURCE_URL, 'concerts/vergangene-konzerte/'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-AT,de;q=0.9,en;q=0.7',
}

# Allegro Vivo is Austrian but occasionally publishes its orchestra's tours.
# These first-party location names identify the foreign occurrences currently
# present in the archive; all other published locations are in Austria.
FOREIGN_CITIES = {
    'Jihlava': 'CZ',
    'Krk': 'HR',
}
COUNTRY_SUFFIXES = {
    'Austria': 'AT',
    'Österreich': 'AT',
    'Croatia': 'HR',
    'Kroatien': 'HR',
    'Czechia': 'CZ',
    'Czech Republic': 'CZ',
    'Tschechien': 'CZ',
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


def parse_map_location(entry, displayed_location):
    venue = displayed_location
    city = ''
    map_element = entry.select_one('.eme-location-map[data-map_text]')
    if map_element:
        map_soup = BeautifulSoup(map_element.get('data-map_text', ''), 'html.parser')
        strong = map_soup.find('strong')
        venue = clean_text(strong) or venue
        if strong:
            strong.decompose()
        location_text = clean_text(map_soup)
        # Events Made Easy renders the address as "street - city".
        pieces = [piece.strip() for piece in location_text.split('\n') if piece.strip()]
        for piece in pieces:
            match = re.search(r'\s+-\s+([^\n]+)$', piece)
            if match:
                city = match.group(1).strip()
                break

    if not city and ',' in displayed_location:
        # On this site the first component is the municipality and the rest is
        # the venue (for example "Horn, Kunsthaus, Arkadenhof").
        parts = [part.strip() for part in displayed_location.split(',')]
        city = parts[0]
        venue = ', '.join(parts[1:])

    return venue.strip(' ,-'), city.strip(' ,-')


def normalize_city_country(city):
    country_code = None
    city = re.sub(r'^\d{4,6}\s+', '', city).strip()
    for suffix, code in COUNTRY_SUFFIXES.items():
        match = re.search(rf'(?:,|\s)\s*{re.escape(suffix)}$', city, re.I)
        if match:
            city = city[:match.start()].strip(' ,')
            country_code = code
            break
    return city, country_code or FOREIGN_CITIES.get(city, 'AT')


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    entry = soup.select_one('main .entry-content')
    title_element = soup.select_one('main h1.entry-title') or soup.select_one('#customheader')
    detail_header = soup.select_one('#customheadersub')
    location_link = soup.select_one('#locationlink')
    if not entry or not title_element or not detail_header or not location_link:
        return None

    header_text = clean_text(detail_header)
    match = re.search(
        r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{4})\s+um\s+(\d{1,2}):(\d{2})',
        header_text,
    )
    if not match:
        return None
    months = {
        'januar': 1, 'februar': 2, 'märz': 3, 'april': 4, 'mai': 5,
        'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
        'oktober': 10, 'november': 11, 'dezember': 12,
    }
    month = months.get(match.group(2).lower())
    if not month:
        return None
    try:
        event_date = datetime(
            int(match.group(3)), month, int(match.group(1))
        ).date().isoformat()
    except ValueError:
        return None

    displayed_location = clean_text(location_link)
    venue, city = parse_map_location(entry, displayed_location)
    city, country_code = normalize_city_country(city)
    if not venue or not city or venue.casefold() == city.casefold():
        return None

    # Retain the full event body because it contains the programme and the
    # expanded editorial notes. Remove only template controls and map/tickets.
    description_root = BeautifulSoup(str(entry), 'html.parser')
    for selector in (
        '#parallaxbg', 'script', 'style', '.eme-location-map', '#mapheading',
        '#tickets', 'a.btn', '.panel-heading',
    ):
        for element in description_root.select(selector):
            element.decompose()
    description = clean_text(description_root)
    description = re.sub(r'(?s)\nTickets\n.*$', '', description).strip()

    return {
        'title': clean_text(title_element),
        'date': event_date,
        'url': url,
        'time_from': f'{int(match.group(4)):02d}:{match.group(5)}',
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AllegroVivoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='allegro_vivo_at',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AT',
        upload_target='classical',
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        event_urls = set()
        for list_url in LIST_URLS:
            response = requests.get(list_url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            main = soup.select_one('main') or soup
            event_urls.update(
                canonical_url(urljoin(list_url, link['href']))
                for link in main.select('a[href*="/de/events/"][href]')
            )

        records = []
        urls = sorted(event_urls)
        # Bound retained responses: archive pages currently link to hundreds of
        # details and each WordPress response includes a large shared template.
        for offset in range(0, len(urls), 12):
            batch = urls[offset:offset + 12]
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {
                    executor.submit(requests.get, url, headers=HEADERS, timeout=45): url
                    for url in batch
                }
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        response = future.result()
                        response.raise_for_status()
                        record = parse_event(response.text, url)
                        if record:
                            records.append(record)
                    except (requests.RequestException, ValueError) as error:
                        log_message(
                            'Failed to scrape Allegro Vivo event detail',
                            event='crawler_item_failed',
                            level='warning',
                            url=url,
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )

        records.sort(key=lambda item: (item['date'], item['time_from'], item['url']))
        return records


def main():
    AllegroVivoCrawler().run()


if __name__ == '__main__':
    main()
