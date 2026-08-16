import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://newportclassical.org/'
SOURCE = 'Newport Classical'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

STATE_RE = re.compile(
    r'(?:^|,)\s*([^,]+?)\s*,?\s+'
    r'(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|'
    r'MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|'
    r'WV|WI|WY|DC|Rhode Island)\b',
    re.IGNORECASE,
)

# A few Recital Hall records expose only the street in their event metadata.
# The venue page and the organization's own footer identify it as being in
# Newport; use that venue-specific fact without applying a general home-city
# default to performances at other locations.
VENUE_CITY_DEFAULTS = {
    'Newport Classical Recital Hall': 'Newport',
    'The Breakers': 'Newport',
    'The Elms': 'Newport',
    'Newport Art Museum': 'Newport',
    'Newport Craft Brewing': 'Newport',
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def clean_text(element):
    if element is None:
        return ''
    if isinstance(element, str):
        element = BeautifulSoup(element, 'html.parser')
    text = element.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value.get('@graph', []) if isinstance(value, dict) else []
        if isinstance(value, dict):
            candidates = [value, *candidates]
        elif isinstance(value, list):
            candidates = value
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_start(value):
    if not value:
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        parsed = None
        for date_format in ('%B %d, %Y %I:%M %p', '%b %d, %Y %I:%M %p'):
            try:
                parsed = datetime.strptime(value, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def schema_venue_and_address(schema):
    location = schema.get('location') if schema else None
    if not isinstance(location, dict):
        return '', ''

    venue = ''
    contained = location.get('containedInPlace')
    if isinstance(contained, dict):
        contained = [contained]
    if isinstance(contained, list):
        for place in contained:
            if isinstance(place, dict) and place.get('name'):
                venue = clean_text(str(place['name']))
                break

    address_values = []
    for place in [location, *(contained or [])]:
        if not isinstance(place, dict):
            continue
        address = place.get('address')
        if isinstance(address, str):
            address_values.append(address)
        elif isinstance(address, dict):
            for key in ('streetAddress', 'addressLocality', 'addressRegion', 'addressCountry'):
                value = address.get(key)
                if isinstance(value, list):
                    address_values.extend(str(item) for item in value)
                elif value:
                    address_values.append(str(value))
    return venue, ', '.join(address_values)


def city_from_address(value):
    if not value:
        return None
    matches = STATE_RE.findall(value)
    if matches:
        city = matches[-1].strip(' ,')
        if city and not any(character.isdigit() for character in city):
            return city
    # Some structured Event data exposes addressLocality without a state.
    value = value.strip(' ,')
    if value.casefold() in {'newport', 'portsmouth', 'middletown', 'bristol'}:
        return value
    return None


def parse_detail(post, response_text):
    soup = BeautifulSoup(response_text, 'html.parser')
    schema = event_schema(soup)

    start = parse_start(schema.get('startDate')) if schema else None
    if start is None:
        start = parse_start(clean_text(soup.select_one('.gt-start-date .gt-inner')))
    if start is None:
        return None

    venue, address = schema_venue_and_address(schema)
    if not venue:
        venue = clean_text(soup.select_one('.gt-venue .gt-inner'))
    html_address = clean_text(soup.select_one('.gt-address .gt-inner'))
    if not venue and '|' in html_address:
        venue = html_address.split('|', 1)[0].strip()
    city = city_from_address(', '.join(part for part in (address, html_address) if part))
    if not city:
        city = VENUE_CITY_DEFAULTS.get(venue)
    if not city or not venue:
        return None

    title = clean_text(post.get('title', {}).get('rendered', ''))
    url = post.get('link', '').strip()
    if not title or not url:
        return None

    description = clean_text(post.get('content', {}).get('rendered', '')) or None
    event_date, event_time = start
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'description': description,
    }


class NewportClassicalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='newportclassical_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def fetch_posts(self, session):
        posts = []
        page = 1
        while True:
            response = session.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    '_fields': 'link,title,content',
                },
                timeout=45,
            )
            response.raise_for_status()
            posts.extend(response.json())
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                return posts
            page += 1

    def scrape(self):
        session = make_session()
        try:
            posts = self.fetch_posts(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Newport Classical event feed',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, post['link'], timeout=45): post
                for post in posts
                if post.get('link')
            }
            for future in as_completed(futures):
                post = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_detail(post, response.text)
                    if record is not None:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Newport Classical event detail',
                        event='crawler_detail_fetch_failed',
                        level='warning',
                        url=post.get('link'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    NewportClassicalOrgCrawler().run()


if __name__ == '__main__':
    main()
