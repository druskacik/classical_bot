import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://loffrandemusicale.fr/'
SOURCE = "L'Offrande Musicale"
PROGRAMME_PAGES = (
    ('programme/', 2021),
    ('programme-2022/', 2022),
    ('programme-2023/', 2023),
    ('programme-2024/', 2024),
    ('programme-2025/', 2025),
    ('programme-2026/', 2026),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}
CITIES = {
    'barbazan debat': 'Barbazan-Debat',
    'bonnemazon': 'Bonnemazon',
    'castelnau riviere basse': 'Castelnau-Rivière-Basse',
    'ibos': 'Ibos',
    'lourdes': 'Lourdes',
    'madiran': 'Madiran',
    'saint savin': 'Saint-Savin',
    'semeac': 'Séméac',
    'tarbes': 'Tarbes',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip(' |')


def folded(value):
    value = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in value if not unicodedata.combining(char))


def parse_dates(value, year):
    text = folded(value).replace('1er', '1')
    numeric = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b', text)
    if numeric:
        parsed_year = int(numeric.group(3))
        if parsed_year < 100:
            parsed_year += 2000
        try:
            return [date(parsed_year, int(numeric.group(2)), int(numeric.group(1)))]
        except ValueError:
            return []
    match = re.search(r'\b(\d{1,2})(?:\s*(?:&|et)\s*(\d{1,2}))?\s+([a-z]+)\b', text)
    if not match or match.group(3) not in MONTHS:
        return []
    results = []
    for day_text in (match.group(1), match.group(2)):
        if not day_text:
            continue
        try:
            results.append(date(year, MONTHS[match.group(3)], int(day_text)))
        except ValueError:
            return []
    return results


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?\b', folded(value))
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def parse_place(value):
    original = clean_text(value)
    normalized = folded(original).replace('-', ' ')
    matches = [(normalized.find(key), key, city) for key, city in CITIES.items() if key in normalized]
    if not matches:
        return None, None
    _, key, city = min(matches)
    # Locations alternate between "city, venue", "venue, city", and
    # "city - venue". Compare folded segments so accents do not prevent the
    # city from being removed from the venue.
    segments = [clean_text(part) for part in re.split(r'\s+[\-–—]\s+|,', original)]
    venue_parts = [part for part in segments if key not in folded(part).replace('-', ' ')]
    venue = clean_text(', '.join(venue_parts))
    if not venue or folded(venue) == folded(city):
        return city, None
    return city, venue


def programme_links(session, page_url):
    response = session.get(page_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return list(dict.fromkeys(
        urljoin(page_url, anchor['href'])
        for anchor in soup.select('a[href*="/show-item/"]')
    ))


def parse_detail(html, url, year):
    soup = BeautifulSoup(html, 'html.parser')
    # The theme used h2 for archive editions and switched to h1 in 2026.
    title_node = soup.select_one('.mkdf-page-title.entry-title')
    header = soup.select_one('.mkdf-title-wrapper')
    content = soup.select_one('.mkdf-container-inner')
    title = clean_text(title_node)
    title = re.sub(
        r'\s*\|\s*(?:\d{1,2}(?:er)?(?:/\d{1,2}/\d{2,4}|(?:\s*(?:&|et)\s*\d{1,2})?\s+[A-Za-zÀ-ÿ]+))\s*$',
        '', title,
        flags=re.I,
    )
    header_text = clean_text(header)
    content_text = clean_text(content)
    dates = parse_dates(header_text, year) or parse_dates(content_text[:250], year)
    if not title or not dates or not header:
        return []

    city = venue = None
    parts = [clean_text(part) for part in header.get_text('|', strip=True).split('|')]
    for part in reversed(parts):
        candidate_city, candidate_venue = parse_place(part)
        if candidate_city:
            city, venue = candidate_city, candidate_venue
            break
    if not city or not venue:
        return []

    if content:
        for node in content.select('script, style, form, nav'):
            node.decompose()
        description = clean_text(content) or None
    else:
        description = None
    return [{
        'title': title,
        'date': event_date.isoformat(),
        'url': url,
        'time_from': parse_time(header_text),
        'venue': venue,
        'city': city,
        'description': description,
    } for event_date in dates]


class LOffrandeMusicaleFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='loffrandemusicale_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )))
        records = []
        seen_urls = set()
        for path, year in PROGRAMME_PAGES:
            page_url = urljoin(SOURCE_URL, path)
            try:
                links = programme_links(session, page_url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch programme page', event='crawler_page_failed', level='warning',
                    url=page_url, error_type=type(error).__name__, error_message=str(error),
                )
                continue
            for url in links:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                try:
                    response = session.get(url, timeout=45)
                    response.raise_for_status()
                    records.extend(parse_detail(response.text, url, year))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch event detail', event='crawler_event_failed', level='warning',
                        url=url, error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    return LOffrandeMusicaleFrCrawler().run()


if __name__ == '__main__':
    main()
