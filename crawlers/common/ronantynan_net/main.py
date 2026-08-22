import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ronantynan.net/'
SOURCE = 'Ronan Tynan'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'

# The host rejects requests that do not send Chromium client-hint headers.
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
    'Accept': 'application/json',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            '', 'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        )
    )
    if name
}

US_STATES = {
    'CT': 'Connecticut', 'DC': 'District of Columbia', 'IN': 'Indiana',
    'MA': 'Massachusetts', 'MD': 'Maryland', 'NH': 'New Hampshire',
    'NJ': 'New Jersey', 'NY': 'New York', 'OH': 'Ohio',
}
STATE_PATTERN = '|'.join(
    sorted((re.escape(value) for value in (*US_STATES, *US_STATES.values())), key=len, reverse=True)
)

EVENT_WORDS = re.compile(
    r'\b(concert|performance|performing|appearances?|show|tour|auditorium)\b', re.I
)
EXCLUDED_WORDS = re.compile(
    r'\b(streaming|livestream|on PBS|will air|televised|inauguration|ceremon(?:y|ies)|'
    r'national anthem|sports?|game|album|CD|DVD|review|article)\b', re.I
)
LOCATION_HINTS = (
    ('Louisville Memorial Auditorium', 'Louisville', 'US'),
    ('Columbus North Erne Auditorium', 'Columbus', 'US'),
    ('National Theatre', 'Washington', 'US'),
    ('West Lawn of the U.S. Capitol', 'Washington', 'US'),
    ('DAR Constitution Hall', 'Washington', 'US'),
    ('Immaculate Conception Parish', 'Marlborough', 'US'),
    ('Umstattd Performance Hall', 'Canton', 'US'),
    ('Palace Theatre in Manchester', 'Manchester', 'US'),
    ('Holy Cross High School', 'Waterbury', 'US'),
    ('Cary Hall', 'Lexington', 'US'),
    ('Patchogue Theatre for Performing Arts', 'Patchogue', 'US'),
    ('Memorial Hall', 'Plymouth', 'US'),
    ('Venus DeMilo', 'Swansea', 'US'),
    ('Reg Lenna Center for the Arts', 'Jamestown', 'US'),
    ('Cape May Convention Hall', 'Cape May', 'US'),
    ('Sandwich High School', 'Sandwich', 'US'),
    ('Conte Forum', 'Boston', 'US'),
)
DATE_PATTERN = re.compile(
    r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?'
    r'(?:\s*,?\s*(20\d{2}))?\b',
    re.I,
)
TIME_PATTERN = re.compile(
    r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?\b', re.I
)


def clean_html(value):
    soup = BeautifulSoup(html.unescape(value or ''), 'html.parser')
    for unwanted in soup.select('script, style, form'):
        unwanted.decompose()
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event_date(text, published):
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    month = MONTHS[match.group(1).lower()]
    year = int(match.group(3)) if match.group(3) else published.year
    try:
        candidate = date(year, month, int(match.group(2)))
    except ValueError:
        return None

    # An undated month/day announcement near year-end normally advertises the
    # next calendar year. Do not infer dates more than one year from publication.
    if not match.group(3) and candidate < published and (published - candidate).days > 45:
        candidate = date(year + 1, month, int(match.group(2)))
    if abs((candidate - published).days) > 370:
        return None
    return candidate.isoformat()


def parse_event_dates(text, published):
    first = parse_event_date(text, published)
    if not first:
        return []
    dates = [first]
    span = re.search(
        r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?\s+'
        r'(?:and|&)\s+(\d{1,2})(?:st|nd|rd|th)?\b',
        text,
        re.I,
    )
    if span:
        try:
            second = date(
                int(first[:4]), MONTHS[span.group(1).lower()], int(span.group(3))
            ).isoformat()
        except ValueError:
            second = None
        if second and second not in dates:
            dates.append(second)
    return dates


def parse_time(text):
    match = TIME_PATTERN.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_city(text):
    for venue, city, country_code in LOCATION_HINTS:
        if venue.casefold() in text.casefold():
            return city, country_code
    patterns = (
        rf'\b(?:in|at)\s+([A-Z][A-Za-z .\'-]+?),\s*({STATE_PATTERN})\b',
        rf'\b([A-Z][A-Za-z .\'-]+?),\s*({STATE_PATTERN})\b',
        r'\bWashington,?\s+D\.?C\.?\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        if pattern.startswith(r'\bWashington'):
            return 'Washington', 'US'
        city = re.sub(r'^(?:the|in|at)\s+', '', match.group(1), flags=re.I).strip(' ,:-')
        city = re.sub(
            r'^(?:performance|concert|school|hall|theatre|auditorium)\s+', '', city,
            flags=re.I,
        )
        if city and len(city.split()) <= 4:
            return city, 'US'
    return None


def parse_venue(text, city):
    for venue, hint_city, _country_code in LOCATION_HINTS:
        if hint_city == city and venue.casefold() in text.casefold():
            return venue.replace(' in Manchester', '')
    escaped_city = re.escape(city)
    patterns = (
        rf'\bat\s+(?:the\s+)?(.{{3,90}}?)\s+in\s+{escaped_city}\b',
        rf'\bat\s+(?:the\s+)?(.{{3,90}}?),\s*{escaped_city}\b',
        rf'\b((?:[A-Z][\w’&.\'-]*\s+){{0,7}}(?:Auditorium|Theatre|Theater|Hall|'
        rf'Center for the Arts|Convention Hall|High School|Parish|Forum))\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        venue = match.group(1).strip(' ,.-')
        venue = re.sub(r'^(?:\d{1,2}(?::\d{2})?\s*[ap]m\s+)?', '', venue, flags=re.I)
        venue = re.sub(
            r'^(?:(?:tickets available today for|performance|concert|appearances?)\s+|at\s+(?:the\s+)?)',
            '', venue,
            flags=re.I,
        )
        if venue and venue.casefold() != city.casefold() and len(venue.split()) <= 12:
            return venue
    return None


def parse_post(post):
    title = clean_html(post.get('title', {}).get('rendered'))
    description = clean_html(post.get('content', {}).get('rendered'))
    combined = f'{title}\n{description}'
    if not title or not EVENT_WORDS.search(combined) or EXCLUDED_WORDS.search(combined):
        return []

    try:
        published = datetime.fromisoformat(post['date']).date()
    except (KeyError, TypeError, ValueError):
        return []
    event_dates = parse_event_dates(combined, published)
    location = parse_city(combined)
    if not event_dates or not location:
        return []
    city, country_code = location
    venue = parse_venue(combined, city)
    if not venue:
        return []

    url = post.get('link')
    if not url:
        return []
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(combined),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
        }
        for event_date in event_dates
    ]


class RonanTynanNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ronantynan_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(
                API_URL,
                params={'per_page': 100, 'page': 1},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            posts = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Ronan Tynan posts',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = [record for post in posts for record in parse_post(post)]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    RonanTynanNetCrawler().run()


if __name__ == '__main__':
    main()
