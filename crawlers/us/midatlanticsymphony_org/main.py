import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://midatlanticsymphony.org/'
SOURCE = 'Mid-Atlantic Symphony Orchestra'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

CITY_BY_VENUE = {
    'academy art museum': 'Easton',
    'cape henlopen high school': 'Lewes',
    'chesapeake college': 'Wye Mills',
    'christ church': 'Easton',
    'community church': 'Ocean Pines',
    'easton church of god': 'Easton',
    'epworth united methodist church': 'Rehoboth Beach',
    'freeman stage': 'Selbyville',
    'ocean city performing arts center': 'Ocean City',
    'performing arts center, ocean city': 'Ocean City',
    'todd performing arts center': 'Wye Mills',
}


def clean_text(value):
    if not value:
        return ''
    text = unescape(str(value)).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    parser = 'xml' if urlparse(url).path.endswith('.xml') else 'html.parser'
    return BeautifulSoup(response.text, parser)


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])M\b', value, re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def city_for(venue, url=''):
    venue_lower = clean_text(venue).lower()
    for needle, city in CITY_BY_VENUE.items():
        if needle in venue_lower:
            return city
    slug = urlparse(url).path.lower()
    for needle, city in (
        ('ocean-pines', 'Ocean Pines'), ('ocean-city', 'Ocean City'),
        ('ocean-view', 'Ocean View'), ('wye-mills', 'Wye Mills'),
        ('easton', 'Easton'), ('lewes', 'Lewes'),
    ):
        if needle in slug:
            return city
    return None


def sitemap_urls(soup):
    return [clean_text(node.get_text()) for node in soup.find_all('loc')]


def parse_schedule(soup, url):
    match = re.search(r'/(\d{4})(\d{4})-season-schedule/?$', url)
    if not match:
        return []
    first_year, second_year = map(int, match.groups())
    main = soup.select_one('main')
    if not main:
        return []

    records = []
    title = None
    description_parts = []
    for node in main.select('h2, h3, p'):
        text = clean_text(node.get_text(' ', strip=True))
        if not text:
            continue
        if node.name == 'h2' and text.rstrip(':').upper() in {'MASTERWORKS', 'ENSEMBLES SERIES'}:
            title = None
            description_parts = []
            continue
        if node.name == 'h2' and title is None:
            title = re.sub(r'^.*?\|\s*', '', text).strip()
            continue

        date_match = re.match(
            r'^[A-Za-z]+,\s+([A-Za-z]+)\s+(\d{1,2}),\s*'
            r'(\d{1,2}(?::\d{2})?\s*[AP]M)\s*(?:\||,)\s*(.+?),\s*'
            r'([A-Za-z .]+),\s*(MD|DE)\s*$',
            text,
            re.I,
        )
        if date_match and title:
            month, day, time_text, venue, city, _state = date_match.groups()
            month_number = datetime.strptime(month, '%B').month
            year = first_year if month_number >= 7 else second_year
            try:
                event_date = datetime(year, month_number, int(day)).date().isoformat()
            except ValueError:
                continue
            venue = clean_text(venue)
            city = clean_text(city)
            if venue and city:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': parse_time(time_text),
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': clean_text('\n'.join(description_parts)) or None,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
            continue
        description_parts.append(text)
    return records


def event_json(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get('@type') == 'Event':
            return payload
    return None


def parse_legacy_event(soup, url):
    payload = event_json(soup)
    if not payload:
        return []
    title_node = soup.select_one('.eventitem-title')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else payload.get('name'))
    title = re.sub(r'\s+—\s+Mid-Atlantic Symphony Orchestra$', '', title)
    start = payload.get('startDate')
    location = payload.get('location') or {}
    venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
    city = city_for(venue, url)
    if not title or not start or not venue or not city:
        return []
    try:
        event_date = datetime.fromisoformat(start).date().isoformat()
    except (TypeError, ValueError):
        return []
    body = soup.select_one('.eventitem-column-content')
    description = clean_text(body.get_text('\n', strip=True) if body else '') or None
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': datetime.fromisoformat(start).strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }]


class MidatlanticsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='midatlanticsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        try:
            urls = sitemap_urls(get_soup(session, SITEMAP_URL))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Mid-Atlantic Symphony sitemap',
                event='crawler_listing_request_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        page_urls = [
            url for url in urls
            if re.search(r'/\d{8}-season-schedule/?$', url)
            or '/concerts/' in urlparse(url).path
        ]
        records = []

        def fetch_page(page_url):
            thread_session = make_session()
            try:
                soup = get_soup(thread_session, page_url)
                if 'season-schedule' in page_url:
                    return parse_schedule(soup, page_url)
                return parse_legacy_event(soup, page_url)
            finally:
                thread_session.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_page, url): url for url in page_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Mid-Atlantic Symphony event page',
                        event='crawler_detail_request_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        log_message(
            'Parsed Mid-Atlantic Symphony events',
            event='crawler_records_parsed',
            record_count=len(records),
        )
        return records


def main():
    return MidatlanticsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
