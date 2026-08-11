import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ncem.co.uk/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
SOURCE = 'National Centre for Early Music'
DEFAULT_VENUE = 'National Centre for Early Music'
DEFAULT_CITY = 'York'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

EVENT_DATE_RE = re.compile(
    r'Event Date:\s*(\d{1,2}/\d{1,2}/\d{4})'
    r'(?:\s+(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm))?',
    re.IGNORECASE,
)

# The programme includes NCEM events and the organisation's festivals in nearby
# towns. These names are taken from first-party event pages; longer names are
# checked first so that a specific hall wins over a containing institution.
VENUE_CITIES = {
    'National Centre for Early Music': 'York',
    'St Margaret’s Church': 'York',
    "St Margaret's Church": 'York',
    'York Minster': 'York',
    'The Quire, York Minster': 'York',
    'St Olave’s Church': 'York',
    "St Olave's Church": 'York',
    'St Lawrence Church': 'York',
    'York Guildhall': 'York',
    'Merchant Adventurers’ Hall': 'York',
    "Merchant Adventurers' Hall": 'York',
    'Sir Jack Lyons Concert Hall': 'York',
    'University of York': 'York',
    'Theatre@41': 'York',
    'York Cemetery Chapel': 'York',
    'Beverley Minster': 'Beverley',
    'St Mary’s Church, Beverley': 'Beverley',
    "St Mary's Church, Beverley": 'Beverley',
    'Toll Gavel Church': 'Beverley',
    'East Riding Theatre': 'Beverley',
    'Beverley Memorial Hall': 'Beverley',
    'Beverley Library': 'Beverley',
    'Bridlington Central Library': 'Bridlington',
    'Goole Library': 'Goole',
    'Market Weighton Library': 'Market Weighton',
    'Pocklington Library': 'Pocklington',
    'Hessle Library': 'Hessle',
    'Unitarian Chapel': 'York',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def event_posts(session):
    records = []
    page = 1
    while True:
        response = get_response(
            session,
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link,title,eventgenre,event_group',
            },
        )
        records.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return records


def normalise_time(hour, minute, meridiem):
    hour = int(hour)
    minute = int(minute or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if meridiem.lower() == 'pm' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def extract_venue(text, event_groups):
    folded = text.casefold().replace('’', "'")
    for venue in sorted(VENUE_CITIES, key=len, reverse=True):
        if venue.casefold().replace('’', "'") in folded:
            return venue, VENUE_CITIES[venue]

    explicit = re.search(r'(?im)^Venue:\s*([^\n|]+)', text)
    if not explicit:
        explicit = re.search(
            r'(?im)^[^\n]*\|\s*([^\n|]*(?:church|chapel|minster|hall|library|'
            r'theatre|centre|center|museum|guildhall)[^\n|]*)$',
            text,
        )
    if explicit:
        venue = re.sub(
            r',?\s*(?:[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}|York|Beverley)\s*$',
            '',
            clean_text(explicit.group(1)),
            flags=re.IGNORECASE,
        ).strip(' ,-')
        if venue.casefold() == 'ncem':
            return DEFAULT_VENUE, DEFAULT_CITY
        if venue:
            if re.search(r'\b(?:York|YO\d{1,2})\b', text, re.IGNORECASE):
                return venue, 'York'
            if re.search(r'\b(?:Beverley|HU17)\b', text, re.IGNORECASE):
                return venue, 'Beverley'

    # The `home` and `external` feeds are performances at NCEM. Touring and
    # festival records are not defaulted, because their venue must be explicit.
    if set(event_groups) & {85, 88}:
        return DEFAULT_VENUE, DEFAULT_CITY
    return None, None


def description_from_page(content):
    blocks = content.select('.main-content .vce-text-block-wrapper')
    parts = []
    for block in blocks:
        text = clean_text(block)
        if not text or text.startswith('Event Date:'):
            continue
        if 'The NCEM is available to hire' in text:
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(post, content):
    soup = BeautifulSoup(content, 'html.parser')
    main_content = soup.select_one('.main-content')
    if not main_content:
        return None

    page_text = clean_text(main_content)
    match = EVENT_DATE_RE.search(page_text)
    title = clean_text(post.get('title', {}).get('rendered'))
    if not match or not title:
        return None

    try:
        event_date = datetime.strptime(match.group(1), '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None

    time_from = None
    if match.group(2) and match.group(4):
        time_from = normalise_time(match.group(2), match.group(3), match.group(4))
        # A number of archived posts use WordPress's midnight placeholder even
        # though the prose advertises another time. Do not publish it as a start.
        if time_from == '00:00':
            time_from = None

    description = description_from_page(main_content)
    venue, city = extract_venue(description or page_text, post.get('event_group', []))
    if not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': post['link'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    posts = event_posts(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_response, session, post['link']): post for post in posts
        }
        for future in as_completed(futures):
            post = futures[future]
            try:
                record = parse_event(post, future.result().content)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape NCEM event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=post['link'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class NcemCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ncem_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
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
    NcemCoUkCrawler().run()


if __name__ == '__main__':
    main()
