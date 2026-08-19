import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.emiliaromagnafestival.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/ajde_events'
SOURCE = 'Emilia Romagna Festival'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# EventON location names normally start with the municipality, but old records
# are inconsistent about capitalisation and punctuation. These are the cities
# found in ERF's geographic programme area and are only used as exact prefixes.
CITIES = sorted({
    'Bagnacavallo', 'Bagnara di Romagna', 'Bologna', 'Borgo Tossignano',
    'Brisighella', 'Casalfiumanese', 'Castel Bolognese',
    'Castel Guelfo di Bologna', 'Castel San Pietro Terme', 'Cesena',
    'Comacchio', 'Conselice', 'Cotignola', 'Dozza', 'Faenza', 'Ferrara',
    'Firenze', 'Fontanelice', 'Forlì', 'Forli', 'Fusignano', 'Imola',
    'Lugo', 'Massa Lombarda', 'Medicina', 'Mordano', 'Ravenna',
    'Riolo Terme', 'Rimini', 'Sasso Morelli', 'Solarolo', 'Tossignano',
}, key=len, reverse=True)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        raw = node.string or node.get_text()
        if 'startDate' not in raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = data.get('@graph', []) if isinstance(data, dict) and '@graph' in data else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_datetime(value):
    if not value:
        return None
    match = re.match(
        r'^(\d{4})-(\d{1,2})-(\d{1,2})(?:T(\d{1,2}):(\d{2}))?',
        str(value),
    )
    if not match:
        return None
    try:
        day = date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None
    time_from = None
    if match[4] is not None and 0 <= int(match[4]) <= 23:
        time_from = f'{int(match[4]):02d}:{match[5]}'
    return day, time_from


def location_values(schema, soup):
    location = schema.get('location')
    if isinstance(location, list):
        location = location[0] if location else {}
    if not isinstance(location, dict):
        location = {}

    name = clean_text(location.get('name'))
    address = location.get('address', {})
    if isinstance(address, dict):
        address = clean_text(address.get('streetAddress') or address.get('addressLocality'))
    else:
        address = clean_text(address)

    location_node = soup.select_one('.event_location_attrs')
    if location_node:
        name = name or clean_text(location_node.get('data-location_name'))
        address = address or clean_text(location_node.get('data-location_address'))
    name_node = soup.select_one('#event_location .evo_location_name, .event_location_name')
    name = name or clean_text(name_node)

    city = None
    postal_city = re.search(
        r'\b\d{5}\s+([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ\' .-]+?)(?:\s+[A-Z]{2})?$',
        address,
    )
    if postal_city:
        city = postal_city.group(1).strip(' ,')

    matched_prefix = None
    normalized_name = re.sub(r'\s+', ' ', name).strip()
    for candidate in CITIES:
        if re.match(rf'^{re.escape(candidate)}(?:\b|\s*,)', normalized_name, re.I):
            city = city or candidate.replace('Forli', 'Forlì')
            matched_prefix = re.match(rf'^{re.escape(candidate)}\s*,?\s*', normalized_name, re.I).group(0)
            break

    if not city:
        prefix = re.match(r'^([A-ZÀ-ÖØ-Þ]+(?:\s+(?:DI|DEL|DELLA|SAN|SANTA|[A-ZÀ-ÖØ-Þ]+))*)\b', normalized_name)
        if prefix:
            city = prefix.group(1).title().replace('Forli', 'Forlì')
            matched_prefix = prefix.group(0)

    venue = normalized_name
    if matched_prefix:
        venue = normalized_name[len(matched_prefix):].strip(' ,-')
    elif city and re.match(rf'^{re.escape(city)}\s*,?\s+', normalized_name, re.I):
        venue = re.sub(rf'^{re.escape(city)}\s*,?\s+', '', normalized_name, count=1, flags=re.I)

    if not city or not venue or venue.casefold() == city.casefold():
        return None
    return venue, city


def parse_detail(post, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return None

    start = parse_datetime(schema.get('startDate'))
    end = parse_datetime(schema.get('endDate'))
    if not start:
        return None
    # EventON used whole-year placeholders on some legacy overview/translation
    # records. A concrete performance from this source never spans multiple days.
    if end and end[0] != start[0]:
        return None

    title = clean_text(schema.get('name')) or clean_text(post.get('title', {}).get('rendered'))
    url = clean_text(schema.get('url')) or clean_text(post.get('link'))
    location = location_values(schema, soup)
    if not title or not url or not location:
        return None

    description_parts = []
    for selector in (
        '.eventon_full_description .eventon_desc_in',
        '#event_customfield3 .evo_custom_content_in',
    ):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)
    if not description_parts:
        text = clean_text(BeautifulSoup(post.get('content', {}).get('rendered', ''), 'html.parser'))
        if text:
            description_parts.append(text)

    venue, city = location
    return {
        'title': title,
        'date': start[0].isoformat(),
        'url': url,
        'time_from': start[1],
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class EmiliaRomagnaFestivalItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='emiliaromagnafestival_it',
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

    def __init__(self, post_limit=None, start_page=1):
        self.post_limit = post_limit
        self.start_page = start_page

    def _posts(self, session):
        posts = []
        page = self.start_page
        while True:
            response = session.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    'orderby': 'id',
                    'order': 'desc',
                    '_fields': 'id,link,title,content',
                },
                timeout=45,
            )
            response.raise_for_status()
            batch = response.json()
            posts.extend(batch)
            if self.post_limit is not None and len(posts) >= self.post_limit:
                return posts[:self.post_limit]
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                return posts
            page += 1

    def _fetch_record(self, post):
        url = post.get('link')
        if not url:
            return None
        try:
            response = requests.get(url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            return parse_detail(post, response.text)
        except (requests.RequestException, TypeError, ValueError) as error:
            log_message(
                'Failed to fetch or parse Emilia Romagna Festival event',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            posts = self._posts(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Emilia Romagna Festival event index',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(self._fetch_record, post) for post in posts]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)
        unique = {}
        for record in records:
            key = (
                record['title'].casefold(), record['date'], record['time_from'],
                record['venue'].casefold(),
            )
            previous = unique.get(key)
            if previous is None or len(record.get('description') or '') > len(previous.get('description') or ''):
                unique[key] = record
        return sorted(unique.values(), key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    EmiliaRomagnaFestivalItCrawler().run()


if __name__ == '__main__':
    main()
