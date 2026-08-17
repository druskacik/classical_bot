import concurrent.futures
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://themiso.org/'
SOURCE = 'Miami Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/conciertos'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?\b', re.IGNORECASE)

CITY_MARKERS = (
    ('Miami Beach', 'Miami Beach'),
    ('Coral Gables', 'Coral Gables'),
    ('Miami Lakes', 'Miami Lakes'),
    ('North Miami', 'North Miami'),
    ('Key Biscayne', 'Key Biscayne'),
    ('Homestead', 'Homestead'),
    ('Doral', 'Doral'),
    ('Miami', 'Miami'),
)

VENUE_CITIES = {
    'adrienne arsht': 'Miami',
    'knight concert hall': 'Miami',
    'james l. knight': 'Miami',
    'miso headquarters': 'Miami',
    'miami design district': 'Miami',
    'flagler street': 'Miami',
    'venetian pool': 'Coral Gables',
    'coral gables museum': 'Coral Gables',
    'fairchild': 'Coral Gables',
    'pinecrest gardens': 'Pinecrest',
    'doral': 'Doral',
    'botanical garden': 'Miami Beach',
    'temple israel': 'Miami',
    'peacock park': 'Miami',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour, minute, period = match.groups()
    hour = int(hour)
    if hour > 12:
        return None
    if period.upper() == 'P' and hour != 12:
        hour += 12
    elif period.upper() == 'A' and hour == 12:
        hour = 0
    return f'{hour:02d}:{int(minute or 0):02d}'


def city_for_venue(venue):
    lowered = venue.lower()
    for marker, city in CITY_MARKERS:
        if marker.lower() in lowered:
            return city
    for marker, city in VENUE_CITIES.items():
        if marker in lowered:
            return city
    return None


def extract_description(soup, excluded):
    parts = []
    for node in soup.select('p, .elementor-widget-text-editor li'):
        text = clean_text(node)
        if not text or text in excluded or text.lower() in {
            'subscription tickets', 'buy tickets', 'tickets', 'playbill',
        }:
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_concert(html, url, api_title=''):
    soup = BeautifulSoup(html, 'lxml')
    title_node = soup.select_one('h1, h2.elementor-heading-title')
    title = clean_text(title_node) or clean_text(api_title)

    headings = [
        clean_text(node) for node in soup.select('h3.elementor-icon-box-title')
        if clean_text(node)
    ]
    page_text = clean_text(soup.get_text(' ', strip=True))
    event_date = next((parse_date(value) for value in headings if parse_date(value)), None)
    if not event_date:
        event_date = parse_date(page_text)

    time_index = next((i for i, value in enumerate(headings) if parse_time(value)), None)
    time_from = parse_time(headings[time_index]) if time_index is not None else None
    venue = ''
    if time_index is not None:
        for candidate in headings[time_index + 1:time_index + 4]:
            if not parse_date(candidate) and not parse_time(candidate):
                venue = candidate
                break

    city = city_for_venue(venue)
    if not all((title, event_date, venue, city)):
        soup.decompose()
        return None

    excluded = {title, venue, *(value for value in headings)}
    record = {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': extract_description(soup, excluded),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    soup.decompose()
    return record


class TheMisoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='themiso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def _get_catalogue(self, session):
        items = []
        page = 1
        while True:
            response = session.get(
                API_URL,
                params={'per_page': 100, 'page': page, '_fields': 'id,link,title'},
                timeout=60,
            )
            if response.status_code == 400 and page > 1:
                break
            response.raise_for_status()
            batch = response.json()
            items.extend(batch)
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1
        return items

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = self._get_catalogue(session)

        def fetch(item):
            url = item.get('link', '')
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                return parse_concert(response.text, url, item.get('title', {}).get('rendered', ''))
            except requests.RequestException as error:
                log_message(
                    'Concert detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            records = [record for record in executor.map(fetch, items) if record]

        log_message(
            'Concert catalogue parsed',
            event='crawler_catalogue_parsed',
            url=API_URL,
            record_count=len(records),
            skipped_count=len(items) - len(records),
        )
        return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


def main():
    TheMisoOrgCrawler().run()


if __name__ == '__main__':
    main()
