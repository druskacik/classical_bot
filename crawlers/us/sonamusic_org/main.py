import html
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sonamusic.org/'
SOURCE = 'Symphony of Northwest Arkansas'
COLLECTION_URLS = [
    urljoin(SOURCE_URL, 'ticketed-performances'),
    urljoin(SOURCE_URL, 'sona-beyond'),
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    key: number
    for number, names in enumerate(
        [
            (), ('jan', 'january'), ('feb', 'february'), ('mar', 'march'),
            ('apr', 'april'), ('may',), ('jun', 'june'), ('jul', 'july'),
            ('aug', 'august'), ('sep', 'sept', 'september'), ('oct', 'october'),
            ('nov', 'november'), ('dec', 'december'),
        ]
    )
    for key in names
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|'
    r'Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?', re.IGNORECASE)

VENUE_CITIES = {
    'Walton Arts Center Atrium': 'Fayetteville',
    'Walton Arts Center': 'Fayetteville',
    'Fayetteville Public Library': 'Fayetteville',
    'Fayetteville Public Square': 'Fayetteville',
    'TheatreSquared': 'Fayetteville',
    'Theatre Squared': 'Fayetteville',
    'Mount Sequoyah': 'Fayetteville',
    'Walmart AMP': 'Rogers',
    'Rogers Public Library': 'Rogers',
    'Hunt Chapel': 'Rogers',
    'Crystal Bridges Museum of American Art': 'Bentonville',
    'Bentonville Public Library': 'Bentonville',
    'Mildred B. Cooper Memorial Chapel': 'Bella Vista',
    'Bella Vista Public Library': 'Bella Vista',
    'Siloam Springs Public Library': 'Siloam Springs',
    'Springdale Public Library': 'Springdale',
    'Presbyterian Church, Historic Cane Hill': 'Cane Hill',
    'Historic Cane Hill': 'Cane Hill',
    'Arkansas Public Theatre': 'Rogers',
    '214 CACHE': 'Springdale',
    'Downtown Fayetteville': 'Fayetteville',
    'Shiloh Museum': 'Springdale',
    'St. Paul’s Episcopal Church': 'Fayetteville',
    "St. Paul's Episcopal Church": 'Fayetteville',
}


def clean_text(markup):
    if not markup:
        return ''
    soup = BeautifulSoup(str(markup), 'html.parser')
    for element in soup.select('style, script, noscript'):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '').replace('\x03', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_url(url):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def event_year(match, item):
    if match.group(3):
        return int(match.group(3))

    url_years = re.findall(r'(?<!\d)(20\d{2})(?!\d)', item.get('urlId', ''))
    if url_years:
        return int(url_years[-1])

    published = datetime.fromtimestamp(item['publishOn'] / 1000, tz=timezone.utc).date()
    event_month = MONTHS[match.group(1).lower()]
    return published.year if event_month >= published.month else published.year + 1


def parse_date(excerpt, item):
    match = DATE_RE.search(excerpt)
    if not match:
        return None, None
    try:
        value = date(
            event_year(match, item),
            MONTHS[match.group(1).lower()],
            int(match.group(2)),
        ).isoformat()
    except (KeyError, TypeError, ValueError, OSError):
        return None, None
    return value, match


def parse_times(excerpt, date_match):
    if date_match is None:
        return [None]
    line_end = excerpt.find('\n', date_match.end())
    date_line = excerpt[date_match.start():line_end if line_end >= 0 else len(excerpt)]
    time_source = date_line
    matches = list(TIME_RE.finditer(time_source))

    if not matches:
        following_lines = excerpt[date_match.end():].splitlines()[:3]
        for line in following_lines:
            if re.fullmatch(
                r'\s*\d{1,2}(?::[0-5]\d)?\s*[ap]\.?m\.?(?:\s*(?:&|-|–)\s*'
                r'\d{1,2}(?::[0-5]\d)?\s*[ap]\.?m\.?)?\s*',
                line,
                re.IGNORECASE,
            ):
                time_source = line
                matches = list(TIME_RE.finditer(time_source))
                break

    if not matches:
        music_time = re.search(r'music starts at\s*([^\n.]+)', excerpt, re.IGNORECASE)
        if music_time:
            time_source = music_time.group(1)
            matches = list(TIME_RE.finditer(time_source))

    if len(matches) > 1:
        between = time_source[matches[0].end():matches[1].start()]
        if re.search(r'[-–]', between) and '&' not in between:
            matches = matches[:1]

    values = []
    for match in matches:
        hour = int(match.group(1)) % 12
        if match.group(3).lower() == 'p':
            hour += 12
        value = f'{hour:02d}:{int(match.group(2) or 0):02d}'
        if value not in values:
            values.append(value)
    return values or [None]


def parse_location(text, collection_url):
    normalized = re.sub(r'\s+', ' ', text)
    for venue, city in VENUE_CITIES.items():
        if venue.lower() in normalized.lower():
            return venue, city

    address = re.search(
        r'\b([A-Z][A-Za-z .\'-]+),\s*(?:Arkansas|AR)\s+\d{5}(?:-\d{4})?\b',
        normalized,
    )
    city = address.group(1).strip() if address else None

    venue_words = re.compile(
        r'\b(?:library|chapel|church|museum|center|centre|hall|theatre|theater|'
        r'auditorium|amphitheater|brewery|school|university|square|house)\b',
        re.IGNORECASE,
    )
    lines = [line.strip(' ,') for line in text.splitlines() if line.strip()]
    venue = next((line for line in lines if venue_words.search(line) and len(line) <= 120), None)
    if venue and city:
        venue = re.sub(r',\s*' + re.escape(city) + r'(?:,.*)?$', '', venue, flags=re.I).strip()
        return venue, city

    # This collection is SoNA's main concert series, whose recurring home venue
    # is Walton Arts Center. Explicit alternate venues are handled above.
    if '/ticketed-performances' in collection_url:
        return 'Walton Arts Center', 'Fayetteville'
    return None


def parse_item(item, collection_url):
    title = clean_text(item.get('title'))
    title = re.sub(r'^Past Event\s*(?:[|┃]\s*)?', '', title, flags=re.IGNORECASE).strip()
    excerpt = clean_text(item.get('excerpt'))
    body = clean_text(item.get('body'))
    event_date, date_match = parse_date(excerpt, item)
    location = parse_location(f'{excerpt}\n{body}', collection_url)
    url = urljoin(SOURCE_URL, item.get('fullUrl', ''))
    if not title or not event_date or not location or not url.startswith(SOURCE_URL):
        return []

    venue, city = location
    description = body or excerpt or None
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in parse_times(excerpt, date_match)
    ]


def fetch_collection(session, collection_url):
    records = []
    page_url = api_url(collection_url)
    seen_urls = set()

    while page_url and page_url not in seen_urls:
        seen_urls.add(page_url)
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch SoNA collection',
                event='crawler_fetch_failed',
                level='error',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        for item in payload.get('items', []):
            records.extend(parse_item(item, collection_url))

        next_url = payload.get('pagination', {}).get('nextPageUrl')
        page_url = api_url(urljoin(SOURCE_URL, next_url)) if next_url else None

    return records


class SonamusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sonamusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for collection_url in COLLECTION_URLS:
            records.extend(fetch_collection(session, collection_url))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    SonamusicOrgCrawler().run()


if __name__ == '__main__':
    main()
