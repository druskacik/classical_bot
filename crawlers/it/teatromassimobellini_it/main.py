import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatromassimobellini.it/'
EVENTS_API_URL = urljoin(SOURCE_URL, 'wp-json/mec/v1/events')
SOURCE = 'Teatro Massimo Bellini di Catania'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

KNOWN_CITIES = (
    'Gravina di Catania', 'Linguaglossa', 'Taormina', 'Siracusa',
    'Acireale', 'Niscemi', 'Ragusa', 'Catania',
)


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
        respect_retry_after_header=True,
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    for node in soup.select('script, style, img, noscript'):
        node.decompose()
    text = soup.get_text('\n', strip=True)
    text = re.sub(r'\[/?cmsmasters_[^\]]*\]', '', text, flags=re.I)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(address, venue):
    haystack = f'{address} {venue}'.casefold()
    for city in KNOWN_CITIES:
        if city.casefold() in haystack:
            return city
    return None


def location_from_api(value):
    text = clean_text(value)
    folded = text.casefold()
    if 'via giuseppe perrotta' in folded:
        return 'Teatro Massimo Bellini', 'Catania'
    if 'via antonino di sangiuliano' in folded:
        return 'Teatro Sangiorgi', 'Catania'
    if 'teatro antico di taormina' in folded:
        return 'Teatro Antico di Taormina', 'Taormina'
    if folded == 'villa bellini':
        return 'Villa Bellini', 'Catania'
    return None


def fetch_venue(url):
    session = make_session()
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    container = soup.select_one('.mec-single-event-location')
    if container is None:
        return None
    venue_node = container.select_one('.author.fn.org')
    address_node = container.select_one('.mec-address')
    venue = clean_text(venue_node) if venue_node else ''
    address = clean_text(address_node) if address_node else ''
    city = city_from_location(address, venue)
    if not venue or not city:
        return None
    return venue.rstrip('.'), city


def explicit_occurrences(description, primary_date):
    """Find additional, explicitly timed performances listed in event prose."""
    text = clean_text(description)
    pattern = re.compile(
        r'(?:(?:luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|'
        r'sabato|domenica)\s+)?'
        r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(\d{4}))?'
        r'(?:(?!\n\n).){0,100}?\bore\s+(\d{1,2})[.:](\d{2})\b',
        re.I | re.S,
    )
    found = []
    primary = date.fromisoformat(primary_date)
    for match in pattern.finditer(text):
        try:
            month = MONTHS[match.group(2).casefold()]
            day = int(match.group(1))
            if match.group(3):
                event_date = date(int(match.group(3)), month, day)
            else:
                candidates = [date(year, month, day) for year in range(primary.year - 1, primary.year + 2)]
                event_date = min(candidates, key=lambda value: abs((value - primary).days))
            hour, minute = int(match.group(4)), int(match.group(5))
        except (KeyError, ValueError):
            continue
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            continue
        # A concrete run belongs close to the structured occurrence. This avoids
        # turning historical dates mentioned in programme prose into events.
        if abs((event_date - primary).days) <= 370:
            found.append((event_date.isoformat(), f'{hour:02d}:{minute:02d}'))
    return found


def api_windows():
    end_year = date.today().year + 5
    yield '2000-01-01T00:00:00', f'{end_year + 1}-01-01T00:00:00'


class TeatroMassimoBelliniItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatromassimobellini_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        events = {}
        for start, end in api_windows():
            params = {
                'show_past_events': 1,
                'show_only_past_events': 0,
                'show_only_one_occurrence': 0,
                'categories': '',
                'locale': 'it',
                'lang': 'it',
                'startParam': start,
                'endParam': end,
                'timeZone': 2,
            }
            try:
                response = session.get(EVENTS_API_URL, params=params, timeout=90)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Teatro Massimo Bellini event feed',
                    event='crawler_fetch_failed',
                    level='error',
                    url=EVENTS_API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            for item in payload:
                key = (item.get('id'), item.get('start'), item.get('url'))
                events[key] = item

        locations = {
            item.get('url'): location_from_api(item.get('location'))
            for item in events.values()
            if item.get('url')
        }
        urls = {url for url, location in locations.items() if location is None}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_venue, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    locations[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Teatro Massimo Bellini event location',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for item in events.values():
            title = clean_text(item.get('title'))
            url = item.get('url')
            start = item.get('start') or ''
            location = locations.get(url)
            if not title or not url or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start[:10]) or not location:
                continue
            try:
                primary_date = date.fromisoformat(start[:10]).isoformat()
            except ValueError:
                continue
            time_match = re.search(r'T(\d{2}):(\d{2})', start)
            time_from = f'{time_match.group(1)}:{time_match.group(2)}' if time_match else None
            description = clean_text(item.get('description')) or None
            venue, city = location
            occurrences = [(primary_date, time_from)]
            occurrences.extend(explicit_occurrences(item.get('description'), primary_date))
            for event_date, event_time in dict.fromkeys(occurrences):
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': event_time,
                    'venue': venue,
                    'city': city,
                    'country_code': 'IT',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    TeatroMassimoBelliniItCrawler().run()


if __name__ == '__main__':
    main()
