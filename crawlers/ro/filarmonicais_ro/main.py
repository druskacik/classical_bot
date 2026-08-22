import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filarmonicais.ro/index.php/ro/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerte')
SOURCE = 'Filarmonica Moldova Iași'
DEFAULT_CITY = 'Iași'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ro-RO,ro;q=0.9,en;q=0.7',
}

# The institution's calendar is based in Iași, but its archive also contains
# touring concerts. These explicit location names must override the home city.
TOUR_CITY_MARKERS = {
    'bucurești': 'București',
    'bucuresti': 'București',
    'bacău': 'Bacău',
    'bacau': 'Bacău',
    'piatra neamț': 'Piatra Neamț',
    'piatra neamt': 'Piatra Neamț',
    'suceava': 'Suceava',
    'vaslui': 'Vaslui',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    """Return every concrete event URL from the combined, newest-first archive."""
    urls = []
    seen = set()
    start = 0
    while True:
        soup = get_soup(session, CONCERTS_URL, {'limit': 100, 'start': start})
        page_urls = []
        for link in soup.select('a.eb-event-title-link[href]'):
            url = urljoin(CONCERTS_URL, link['href'])
            if url not in seen:
                seen.add(url)
                page_urls.append(url)
        if not page_urls:
            break
        urls.extend(page_urls)
        if len(page_urls) < 100:
            break
        start += 100
    return urls


def property_rows(root):
    values = {}
    for row in root.select('table tr'):
        cells = row.find_all(['th', 'td'])
        if len(cells) >= 2:
            values[clean_text(cells[0]).lower()] = clean_text(cells[1])
    return values


def parse_occurrence(value):
    match = re.search(
        r'(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?', value or ''
    )
    if not match:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), int(match.group(2)), int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        hour = int(match.group(4))
        minute = int(match.group(5))
        if hour > 23 or minute > 59:
            return None, None
        time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def resolve_city(venue):
    normalized = venue.casefold()
    for marker, city in TOUR_CITY_MARKERS.items():
        if marker in normalized:
            return city
    return DEFAULT_CITY


def description_text(root):
    # This node contains the complete editorial body (including repertoire),
    # while its parent also contains event metadata and social controls.
    description = root.select_one('.eb-description-details')
    if not description:
        return None
    description = BeautifulSoup(str(description), 'html.parser')
    for element in description.select('h1, .eb-page-heading, script, style'):
        element.decompose()
    text = clean_text(description)
    return text or None


def parse_event(session, url):
    soup = get_soup(session, url)
    root = soup.select_one('#eb-event-page')
    if not root:
        return None
    title = clean_text(root.select_one('h1.eb-page-heading') or soup.select_one('h1'))
    properties = property_rows(root)
    occurrence = next(
        (value for key, value in properties.items() if 'data evenimentului' in key), ''
    )
    venue = next((value for key, value in properties.items() if 'loca' in key), '')
    event_date, time_from = parse_occurrence(occurrence)
    if not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': resolve_city(venue),
        'country_code': 'RO',
        'description': description_text(root),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    listing_session = requests.Session()
    listing_session.headers.update(HEADERS)
    urls = listing_urls(listing_session)
    records = []

    def fetch(url):
        session = requests.Session()
        session.headers.update(HEADERS)
        return parse_event(session, url)

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FilarmonicaIsRoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonicais_ro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='classical',
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

    def scrape(self):
        return get_concerts()


def main():
    FilarmonicaIsRoCrawler().run()


if __name__ == '__main__':
    main()
