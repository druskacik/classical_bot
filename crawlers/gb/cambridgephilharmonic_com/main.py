import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cambridgephilharmonic.com/'
SOURCE = 'Cambridge Philharmonic'
CALENDAR_URLS = (
    urljoin(SOURCE_URL, 'whats-on/'),
    urljoin(SOURCE_URL, 'whats-on/archives/'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
MONTH_PATTERN = (
    r'January|February|March|April|May|June|July|August|September|'
    r'October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec'
)
DATE_RE = re.compile(
    rf'\b(?P<day>\d{{1,2}})\s*(?:st|nd|rd|th)?\s+'
    rf'(?P<month>{MONTH_PATTERN})[,.]?\s+(?P<year>20\d{{2}})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    month = match.group('month')
    if month.lower() == 'sept':
        month = 'Sep'
    try:
        return datetime.strptime(
            f"{match.group('day')} {month[:3]} {match.group('year')}",
            '%d %b %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_times(text):
    times = []
    for match in TIME_RE.finditer(text):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3).lower()
        if not 1 <= hour <= 12 or minute > 59:
            continue
        if meridiem == 'pm' and hour != 12:
            hour += 12
        elif meridiem == 'am' and hour == 12:
            hour = 0
        value = f'{hour:02d}:{minute:02d}'
        if value not in times:
            times.append(value)

    # The site sometimes abbreviates a pair as "2 & 4pm".
    pair = re.search(
        r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(?:&|and)\s*'
        r'(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b',
        text,
        re.IGNORECASE,
    )
    if pair:
        first = f"{pair.group(1)}{':' + pair.group(2) if pair.group(2) else ''}{pair.group(5)}"
        for value in reversed(parse_times(first)):
            if value not in times:
                times.insert(0, value)
    return times or [None]


def venue_and_city(text, date_match):
    before = text[:date_match.start()].strip(' ,.-\n')
    before = before.split('\n')[-1].strip(' ,.-')
    after = text[date_match.end():].strip(' ,.-\n')
    after = TIME_RE.sub('', after)
    after = re.sub(r'^[\s,&]*(?:and\s+)?', '', after, flags=re.IGNORECASE)
    location = before or after
    location = re.sub(
        r'\b(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|'
        r'Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b',
        '',
        location,
        flags=re.IGNORECASE,
    )
    location = re.sub(r'\s+', ' ', location).strip(' ,.-')
    location = re.sub(r'^(?:at\s+)', '', location, flags=re.IGNORECASE).strip(' ,.-')
    parts = [part.strip() for part in location.split(',') if part.strip()]
    if len(parts) >= 2:
        return ', '.join(parts[:-1]), parts[-1]
    if location:
        # These are the only venue-only forms observed on the first-party feed.
        known_cities = {
            'West Road Concert Hall': 'Cambridge',
            'Saffron Hall': 'Saffron Walden',
            'Downing Place URC': 'Cambridge',
            'Downing Place United Reformed Church': 'Cambridge',
            'Wesley Methodist Church': 'Cambridge',
        }
        city = known_cities.get(location)
        if city:
            return location, city
    return None, None


def listing_items(soup, page_url):
    items = []
    seen = set()
    for heading in soup.select(
        'main h1 a[href], main h2 a[href], main h3 a[href], '
        'main h4 a[href], main h5 a[href], main h6 a[href]'
    ):
        title = clean_text(heading)
        url = urljoin(page_url, heading.get('href', ''))
        if not title or url in seen or urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
            continue
        container = heading
        metadata = ''
        for _ in range(7):
            container = container.parent
            if not container:
                break
            candidates = [clean_text(node) for node in container.select('p')]
            date_index = next(
                (index for index, value in enumerate(candidates) if DATE_RE.search(value)),
                None,
            )
            if date_index is not None:
                metadata = candidates[date_index]
                if date_index + 1 < len(candidates):
                    metadata += '\n' + candidates[date_index + 1]
            if metadata:
                break
        if not metadata:
            continue
        match = DATE_RE.search(metadata)
        event_date = parse_date(metadata)
        venue, city = venue_and_city(metadata, match)
        if event_date and venue and city:
            seen.add(url)
            items.append((title, event_date, url, venue, city, parse_times(metadata)))
    return items


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = []
    for page_url in CALENDAR_URLS:
        items.extend(listing_items(get_soup(session, page_url), page_url))

    records = []
    details = {}
    for title, event_date, url, venue, city, times in items:
        if url not in details:
            try:
                detail = get_soup(session, url)
                details[url] = clean_text(detail.select_one('main')) or None
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Cambridge Philharmonic event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                details[url] = None
        for time_from in times:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'GB',
                'description': details[url],
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    unique = {(r['url'], r['date'], r['time_from']): r for r in records}
    return sorted(unique.values(), key=lambda r: (r['date'], r['time_from'] or '', r['title']))


class CambridgePhilharmonicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cambridgephilharmonic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    CambridgePhilharmonicComCrawler().run()


if __name__ == '__main__':
    main()
