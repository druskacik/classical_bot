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


SOURCE_URL = 'https://gulfcoastsymphony.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concert'
SOURCE = 'Gulf Coast Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH_RE = re.compile(
    r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|'
    r'Dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(\d{1,2})(?::(\d{2}))?\s*(?:([AP])\.?M\.?)?', re.IGNORECASE
)

VENUE_CITIES = {
    'arcade theatre of florida rep': 'Fort Myers',
    'barbara b mann pah': 'Fort Myers',
    'barbara b. mann performing arts hall': 'Fort Myers',
    'big arts sanibel': 'Sanibel',
    'luminary hotel caloosa sound convention center': 'Fort Myers',
    'macc outdoor pavilion': 'Fort Myers',
    'music & arts community center': 'Fort Myers',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def listing_records(session):
    response = session.get(CONCERTS_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    grid = soup.select_one('#concertsGrid[data-concerts]')
    if not grid:
        return {}
    try:
        items = json.loads(grid['data-concerts'])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return {int(item['id']): item for item in items if item.get('id')}


def api_concerts(session):
    records = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,date,link,title,content,location,genre',
            },
            timeout=60,
        )
        if response.status_code == 400 and records:
            break
        response.raise_for_status()
        batch = response.json()
        records.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return records


def inferred_year(month, day, published, explicit_year=None, listing_dates=()):
    if explicit_year:
        return int(explicit_year)
    for value in listing_dates:
        try:
            parsed = datetime.strptime(value, '%a %b %d %Y')
        except (TypeError, ValueError):
            continue
        if parsed.month == month and parsed.day == day:
            return parsed.year
    year = published.year
    candidate = datetime(year, month, day)
    if (candidate - published).days < -45:
        year += 1
    return year


def parse_occurrences(value, published, listing_dates=()):
    text = clean_text(value).replace('a.m.', 'AM').replace('p.m.', 'PM')
    matches = list(MONTH_RE.finditer(text))
    occurrences = []
    for index, match in enumerate(matches):
        month = datetime.strptime(match.group(1)[:3], '%b').month
        day = int(match.group(2))
        try:
            year = inferred_year(month, day, published, match.group(3), listing_dates)
            event_date = datetime(year, month, day).date().isoformat()
        except ValueError:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        time_matches = [
            item for item in TIME_RE.finditer(text[match.end():end])
            if item.group(2) or item.group(3)
        ]
        default_meridiem = next(
            (item.group(3) for item in reversed(time_matches) if item.group(3)), None
        )
        times = []
        for time_match in time_matches:
            meridiem = time_match.group(3) or default_meridiem
            if not meridiem:
                continue
            hour = int(time_match.group(1)) % 12
            if meridiem.upper() == 'P':
                hour += 12
            times.append(f'{hour:02d}:{int(time_match.group(2) or 0):02d}')
        for event_time in times or [None]:
            occurrences.append((event_date, event_time))
    return occurrences


def parse_venue(value):
    text = clean_text(value)
    if not text or text.lower().startswith('virtual'):
        return None, None
    parts = [part.strip() for part in text.split('|') if part.strip()]
    venue = parts[0]
    city = parts[-1] if len(parts) > 1 else VENUE_CITIES.get(venue.lower())
    if not city:
        return None, None
    return venue, city


def parse_concert(item, listing_item, session):
    url = item.get('link', '')
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    date_node = soup.select_one('.concertFeatured-date')
    venue_node = soup.select_one('.sc-location-title')
    venue, city = parse_venue(venue_node.get_text(' ', strip=True) if venue_node else '')
    if not date_node or not venue or not city:
        return []

    published = datetime.fromisoformat(item['date'])
    listing_dates = (listing_item or {}).get('dateArr', [])
    occurrences = parse_occurrences(date_node.get_text(' ', strip=True), published, listing_dates)
    if not occurrences:
        return []

    title = clean_text(item.get('title', {}).get('rendered'))
    description_node = soup.select_one('.concertIntro .content')
    description = clean_text(description_node) if description_node else clean_text(
        item.get('content', {}).get('rendered')
    )
    if not title or not url:
        return []
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date, event_time in occurrences]


def scrape_concerts():
    session = make_session()
    current_listing = listing_records(session)
    items = api_concerts(session)
    records = []

    def fetch(item):
        worker_session = make_session()
        try:
            return parse_concert(item, current_listing.get(item['id']), worker_session)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=item.get('link'),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return []
        finally:
            worker_session.close()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch, item) for item in items]
        for future in as_completed(futures):
            records.extend(future.result())

    session.close()
    if not records:
        log_message(
            'No parseable concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class GulfCoastSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gulfcoastsymphony_org',
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
        return scrape_concerts()


def main():
    GulfCoastSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
