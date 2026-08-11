import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musicinverness.uk/'
SOURCE = 'musicinverness'
API_URL = 'https://public-api.wordpress.com/rest/v1.1/sites/musicinverness.uk/posts/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# The diary covers Inverness and communities across the Scottish Highlands.
# Match explicit place evidence rather than applying Inverness as a blanket default.
PLACE_RULES = [
    (r'boat of garten', 'Boat of Garten'),
    (r'newtonmore', 'Newtonmore'),
    (r'strathpeffer', 'Strathpeffer'),
    (r'pluscarden', 'Elgin'),
    (r'findhorn|universal hall', 'Findhorn'),
    (r'forres|logie steading', 'Forres'),
    (r'cromarty|the stables causeway', 'Cromarty'),
    (r'nairn', 'Nairn'),
    (r'resolis', 'Resolis'),
    (r'nethy bridge', 'Nethy Bridge'),
    (r'evanton', 'Evanton'),
    (r'inverness|eden court|culduthel|midmills|crown church', 'Inverness'),
]

VENUE_PATTERNS = [
    r'Nairn Community (?:&|and) Arts Centre',
    r'Boat of Garten (?:Community|Memorial) Hall',
    r'Inverness Town House', r'Inverness Cathedral',
    r'(?:Empire|OneTouch|One Touch|The Chapel) Theatre,? Eden Court',
    r'Eden Court(?: Theatre)?', r'Universal Hall(?:,? Findhorn)?',
    r'Strathpeffer Pavilion', r'Logie Steading',
    r'Midmills (?:Parish )?Church(?: \(Crown Church\))?',
    r'Crown Church', r'Culduthel Christian Centre',
    r'The Stables(?: Causeway)?', r'Resolis (?:Community|Memorial) Hall',
    r'Nairn Old Parish Church', r'Pluscarden Abbey',
    r'St Bride[’\']s Church', r'Nethy Bridge Community Hall',
    r'Evanton Community Wood', r'Free North Church',
    r'Cromarty Victoria Hall',
]

DATE_PATTERNS = [
    re.compile(
        r'\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?'
        r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
        r'(?:\s*,?\s*(?P<year>20\d{2}))?', re.I,
    ),
    re.compile(
        r'\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>20\d{2}))?', re.I,
    ),
]


def clean_text(value):
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    value = html.unescape(str(value or '')).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(text, published_date):
    matches = []
    for pattern in DATE_PATTERNS:
        matches.extend(pattern.finditer(text))
    if not matches:
        return None
    match = min(matches, key=lambda item: item.start())
    year = int(match.group('year')) if match.group('year') else int(published_date[:4]) + 1
    try:
        return date(year, MONTHS[match.group('month').lower()], int(match.group('day'))).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(
        r'(?<!\d)([01]?\d|2[0-3])(?:[.:]([0-5]\d))?\s*(a\.?m\.?|p\.?m\.?)\b', text, re.I
    )
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).lower().startswith('p'):
            hour += 12
        return f'{hour:02d}:{match.group(2) or "00"}'
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])[.:]([0-5]\d)(?!\d)', text)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def extract_city(text):
    lowered = text.lower()
    for pattern, city in PLACE_RULES:
        if re.search(pattern, lowered):
            return city
    return None


def extract_venue(text):
    for pattern in VENUE_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match:
            return clean_text(match.group(0)).rstrip(' ,.-')
    # Most event headings begin "Venue, weekday/date". Keep only a short,
    # venue-like prefix and reject date-only or region-wide descriptions.
    date_starts = [match.start() for pattern in DATE_PATTERNS for match in pattern.finditer(text)]
    if not date_starts:
        return None
    prefix = text[:min(date_starts)].strip(' ,.-:')
    prefix = re.sub(r'^(?:on\s+|at\s+|a candlelit concert:\s*)', '', prefix, flags=re.I)
    prefix = re.sub(r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b.*$', '', prefix, flags=re.I)
    prefix = prefix.strip(' ,.-:')
    if (
        not prefix
        or len(prefix) > 100
        or re.search(r'\b(?:locations|venues|highlands|times vary|scheduled for)\b', prefix, re.I)
        or not re.search(
            r'\b(?:hall|church|theatre|cathedral|pavilion|abbey|centre|house|wood|stables|brewery)\b',
            prefix, re.I,
        )
    ):
        return None
    return prefix


def detail_line(soup):
    candidates = soup.find_all(['h2', 'h3', 'h4', 'h5', 'p'])
    for node in candidates:
        text = clean_text(node)
        if any(pattern.search(text) for pattern in DATE_PATTERNS) and extract_city(text):
            return text
    return ''


def parse_post(post):
    title = clean_text(BeautifulSoup(post.get('title', ''), 'html.parser'))
    if not title or re.match(r'^musicinverness\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)', title, re.I):
        return None
    if re.search(r'\b(?:season tickets?|applications? open|festival programme)\b', title, re.I):
        return None

    soup = BeautifulSoup(post.get('content', ''), 'html.parser')
    line = detail_line(soup)
    event_date = parse_date(line, post.get('date', '')) if line else None
    city = extract_city(line)
    venue = extract_venue(line)
    url = str(post.get('URL') or '').replace('http://musicinverness.uk/', SOURCE_URL)
    if not all((title, event_date, url, venue, city)):
        return None

    description = clean_text(soup)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(line),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MusicInvernessUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicinverness_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        offset = 0
        while True:
            try:
                response = session.get(
                    API_URL, params={'number': 100, 'offset': offset}, timeout=60
                )
                response.raise_for_status()
                posts = response.json().get('posts', [])
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch musicinverness posts',
                    event='crawler_fetch_failed', level='error', url=API_URL,
                    error_type=type(error).__name__, error_message=str(error),
                )
                raise
            for post in posts:
                record = parse_post(post)
                if record:
                    records.append(record)
            if len(posts) < 100:
                break
            offset += len(posts)
        log_message(
            'Parsed musicinverness candidate events',
            event='crawler_parse_completed', level='info', record_count=len(records),
        )
        return records


def main():
    return MusicInvernessUkCrawler().run()


if __name__ == '__main__':
    main()
