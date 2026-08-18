import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://barefootopera.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'
SOURCE = 'Barefoot Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        1,
    )
}
MONTH_PATTERN = '|'.join(
    [name for name in MONTHS] + [name[:3] for name in MONTHS]
)
DATE_RE = re.compile(
    rf'\b(?:(?P<day1>\d{{1,2}})\s*(?:st|nd|rd|th)?\s+(?P<month1>{MONTH_PATTERN})'
    rf'|(?P<month2>{MONTH_PATTERN})\s+(?P<day2>\d{{1,2}})\s*(?:st|nd|rd|th)?)'
    r'(?:\s*,?\s*(?P<year>20\d{2}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)
TIME_24_RE = re.compile(r'\b(?:time\s*[-:]\s*)?([01]?\d|2[0-3]):([0-5]\d)\b', re.IGNORECASE)
TIME_RANGE_RE = re.compile(
    r'\b(\d{1,2})(?:[.:](\d{2}))?\s*[-–]\s*\d{1,2}(?:[.:]\d{2})?\s*(am|pm)\b',
    re.IGNORECASE,
)

# The event pages use free-text locations. These are first-party venue/city pairs
# seen in the catalogue, including the company's touring performances.
LOCATIONS = (
    ('norden farm centre for the arts', 'Norden Farm Centre for the Arts', 'Maidenhead', 'GB'),
    ('norden farm arts centre', 'Norden Farm Arts Centre', 'Maidenhead', 'GB'),
    ('norden farm', 'Norden Farm', 'Maidenhead', 'GB'),
    ('crowhurst place', 'Crowhurst Place', 'Lingfield', 'GB'),
    ('big wood estate', 'Big Wood Estate', 'Lingfield', 'GB'),
    ('st john the evangelist', 'St John the Evangelist', 'St Leonards-on-Sea', 'GB'),
    ('st. john’s the evangelist', 'St John the Evangelist', 'St Leonards-on-Sea', 'GB'),
    ('st. john\'s the evangelist', 'St John the Evangelist', 'St Leonards-on-Sea', 'GB'),
    ('st johns church hall', "St John's Church Hall", 'St Leonards-on-Sea', 'GB'),
    ('the music room', 'The Music Room', 'St Leonards-on-Sea', 'GB'),
    ('the uplands', 'The Uplands', 'St Leonards-on-Sea', 'GB'),
    ('6 the uplands', '6 The Uplands', 'St Leonards-on-Sea', 'GB'),
    ('battle abbey', 'Battle Abbey', 'Battle', 'GB'),
    ('arcola theatre', 'Arcola Theatre', 'London', 'GB'),
    ('rye creative centre', 'Rye Creative Centre', 'Rye', 'GB'),
    ('findon place', 'Findon Place', 'Findon', 'GB'),
    ('down house', 'Down House', 'Lamberhurst', 'GB'),
    ('st. michael and all angels', 'St Michael and All Angels', 'Brighton', 'GB'),
    ('st michael and all angels', 'St Michael and All Angels', 'Brighton', 'GB'),
    ('wooburn arts festival', 'Wooburn Arts Festival', 'Bourne End', 'GB'),
    ('wooburn festival', 'Wooburn Festival', 'Bourne End', 'GB'),
    ('la basse passiere', 'La Basse Passiere', 'Perche en Noce', 'FR'),
    ('la basse passière', 'La Basse Passière', 'Perche en Nocé', 'FR'),
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalise_time(text):
    match = TIME_24_RE.search(text)
    if match:
        return f'{int(match.group(1)):02d}:{int(match.group(2)):02d}'
    match = TIME_RANGE_RE.search(text) or TIME_RE.search(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def location_from_text(text):
    lowered = text.lower()
    for needle, venue, city, country_code in LOCATIONS:
        if needle in lowered:
            return venue, city, country_code
    return None


def event_year(item, page_text, match):
    if match.group('year'):
        return int(match.group('year'))
    title = clean_text(item.get('title', {}).get('rendered'))
    title_year = re.search(r'\b(20\d{2})\b', title)
    if title_year:
        return int(title_year.group(1))
    years = set(re.findall(r'\b20\d{2}\b', page_text))
    if len(years) == 1:
        return int(years.pop())
    return int(item['date'][:4])


def parse_occurrences(item):
    content_html = item.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(content_html, 'html.parser')
    description = clean_text(soup) or None
    title = clean_text(item.get('title', {}).get('rendered'))
    url = item.get('link', '').rstrip(';')
    if not title or not url:
        return []

    blocks = []
    for node in soup.select('p, li, td'):
        text = clean_text(node)
        for line in text.splitlines():
            line = line.strip()
            if line and line not in blocks:
                blocks.append(line)

    records = []
    for index, block in enumerate(blocks):
        matches = list(DATE_RE.finditer(block))
        if not matches:
            continue

        nearby = '\n'.join(blocks[max(0, index - 1):index + 4])
        location = location_from_text(block)
        if not location and (
            re.search(r'\bdate\s*[-–:]', block, re.IGNORECASE)
            or (len(block) < 100 and normalise_time(block))
        ):
            location = location_from_text(nearby)
        if not location:
            continue
        venue, city, country_code = location

        for match in matches:
            month_name = match.group('month1') or match.group('month2')
            month = MONTHS[month_name[:3].lower() + next(
                (name[3:] for name in MONTHS if name.startswith(month_name[:3].lower())), '')
            ] if len(month_name) == 3 else MONTHS[month_name.lower()]
            day = int(match.group('day1') or match.group('day2'))
            year = event_year(item, description or '', match)
            try:
                event_date = date(year, month, day).isoformat()
            except ValueError:
                continue
            records.append(
                {
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': normalise_time(block) or normalise_time(nearby),
                    'venue': venue,
                    'city': city,
                    'country_code': country_code,
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                }
            )
    return records


def get_events():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = []
    page = 1
    while True:
        try:
            response = session.get(
                API_URL,
                params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
                timeout=45,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Barefoot Opera event API',
                event='crawler_page_failed',
                level='warning',
                url=API_URL,
                page=page,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        items.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', 1))
        if page >= total_pages:
            break
        page += 1

    records = []
    for item in items:
        records.extend(parse_occurrences(item))
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['venue']),
    )


class BarefootOperaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='barefootopera_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_events()


def main():
    BarefootOperaComCrawler().run()


if __name__ == '__main__':
    main()
