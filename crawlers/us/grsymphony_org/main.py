import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.grsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-and-tickets')
SOURCE = 'Grand Rapids Symphony'
COUNTRY_CODE = 'US'
DEFAULT_CITY = 'Grand Rapids'
ARCHIVE_START = '2000-01-01'
ARCHIVE_END = '2100-12-31'
PAGE_SIZE = 500
DETAIL_WORKERS = 12

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*@\s*'
    r'(\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.IGNORECASE,
)

# These are the non-local venues which occur in the first-party archive without
# an address. All other named physical venues are defensibly local to this
# Grand Rapids-based calendar unless an address supplies another city.
VENUE_CITIES = {
    'Blue Lake Fine Arts Camp': 'Twin Lake',
    'Blue Lake Public Radio': 'Twin Lake',
    'Cannonsburg Ski Area': 'Belmont',
    'Carnegie Hall': 'New York',
    'Chenery Auditorium': 'Kalamazoo',
    'Dogwood Center for Performing Arts': 'Fremont',
    'Dogwood Center for the Performing Arts': 'Fremont',
    'Great Lakes Center for the Arts': 'Bay Harbor',
    'Greenville High School': 'Greenville',
    'Hastings Performing Arts Center': 'Hastings',
    'Hill Auditorium': 'Ann Arbor',
    'Hope college': 'Holland',
    'Jack H. Miller Center for Musical Arts': 'Holland',
    'Thornapple Plaza, Hastings': 'Hastings',
    'Williams Auditorium - Ferris State University': 'Big Rapids',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def calendar_configuration(html):
    values = {}
    patterns = {
        'api_url': r"apiUrl:\s*['\"]([^'\"]+)",
        'token': r"apiToken:\s*['\"]([^'\"]+)",
        'site_id': r'siteId:\s*(\d+)',
        'page_part_id': r'pagePartId:\s*(\d+)',
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, html)
        if not match:
            raise ValueError(f'Calendar configuration is missing {name}')
        values[name] = match.group(1)
    return values


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return '', None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return '', None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            event_time = datetime.strptime(match.group(2).upper(), pattern).strftime('%H:%M')
            return event_date, event_time
        except ValueError:
            pass
    return event_date, None


def parse_location(node):
    raw = clean_text(node).replace('\n', ' ')
    raw = re.sub(r'\s+', ' ', raw).strip()
    if not raw or re.search(r'\b(?:online|virtual) event\b', raw, re.IGNORECASE):
        return '', ''

    venue = clean_text(raw.split('|', 1)[0])
    if not venue or venue.lower() == DEFAULT_CITY.lower() or re.fullmatch(r'\d+\s+.+', venue):
        return '', ''

    city = ''
    if '|' in raw:
        address = raw.split('|', 1)[1]
        match = re.search(r'-\s*([^,|]+)\s*,\s*[A-Z]{2}\b', address)
        if match:
            city = clean_text(match.group(1))
            if city == 'Grand Rapids Charter Township':
                city = 'Grand Rapids'
    if not city:
        for known_venue, known_city in VENUE_CITIES.items():
            if known_venue.lower() in venue.lower():
                city = known_city
                break
    return venue, city or DEFAULT_CITY


def parse_card(card):
    title = clean_text(card.select_one('.CalendarListEvent__title'))
    event_date, time_from = parse_date_time(card.select_one('.CalendarListEvent__date_time'))
    venue, city = parse_location(card.select_one('.CalendarListEvent__location'))
    link = card.select_one('.CalendarListEvent__description a[href]')
    url = urljoin(SOURCE_URL, link.get('href')) if link else ''
    description_node = card.select_one('.CalendarListEvent__description')
    if description_node and link:
        link.extract()
    description = clean_text(description_node) or None

    if not title or not event_date or not venue or not city or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(url):
    if urlparse(url).netloc.lower() not in {'grsymphony.org', 'www.grsymphony.org'}:
        return None
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('.templatecontent')
        return clean_text(content) or None
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_request_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


def enrich_descriptions(records):
    # Archive detail pages are frequently retired, while the calendar retains
    # their useful description excerpt. Enrich live/future events from their
    # maintained pages without issuing hundreds of requests for dead pages.
    today = date.today().isoformat()
    urls = sorted({record['url'] for record in records if record['date'] >= today})
    descriptions = {}
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
        futures = {executor.submit(detail_description, url): url for url in urls}
        for future in as_completed(futures):
            descriptions[futures[future]] = future.result()
    for record in records:
        record['description'] = descriptions.get(record['url']) or record['description']


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    listing = session.get(LISTING_URL, timeout=45)
    listing.raise_for_status()
    configuration = calendar_configuration(listing.text)
    endpoint = (
        f"{configuration['api_url'].rstrip('/')}"
        f"/pageparts/calendars/{configuration['page_part_id']}/render"
    )
    params = {
        'token': configuration['token'],
        'siteid': configuration['site_id'],
        'start': ARCHIVE_START,
        'end': ARCHIVE_END,
        'sortBy': 'startDate',
        'pageSize': PAGE_SIZE,
        'days': (date(2100, 12, 31) - date(2000, 1, 1)).days,
    }

    records = []
    page = 1
    while True:
        params['page'] = page
        response = session.get(endpoint, params=params, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.json()['Content'], 'html.parser')
        cards = soup.select('.CalendarListEvent')
        for card in cards:
            record = parse_card(card)
            if record:
                records.append(record)
        if len(cards) < PAGE_SIZE:
            break
        page += 1

    enrich_descriptions(records)
    records.sort(key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not records:
        log_message(
            'No parseable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return records


class GrsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='grsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    GrsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
