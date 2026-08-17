import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.arlingtonphilharmonic.org/'
CALENDAR_URL = 'https://www.arlingtonphilharmonic.org/concerts.html'
SOURCE = 'Arlington Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
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

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:,\s*(20\d{2}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?'
    r'(?:\s*[-–]\s*(?:1[0-2]|0?[1-9])(?::[0-5]\d)?)?\s*([ap])\.?m\.?',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'The\s+(20\d{2})-(20\d{2})\s+Arlington Philharmonic Season', re.I)

# The calendar frequently omits the city after well-known local venues. These
# mappings are based on the venue names and explicit locations elsewhere on the
# same first-party calendar.
VENUES = (
    ('Washington-Liberty High School Auditorium', 'Arlington'),
    ('Washington-Liberty High School', 'Arlington'),
    ('Washington-Liberty Auditorium', 'Arlington'),
    ('Williamsburg Middle School Black Box Theater', 'Arlington'),
    ('Williamsburg Middle School Auditorium', 'Arlington'),
    ('Williamsburg Middle School auditorium', 'Arlington'),
    ('Williamsburg Middle School', 'Arlington'),
    ('Kenmore Middle School Black Box Theater', 'Arlington'),
    ('Gunston Arts Center, Theater One', 'Arlington'),
    ('Lubber Run Amphitheater', 'Arlington'),
    ('Lubber Run Amphitheatre', 'Arlington'),
    ('National Landing Water Park', 'Arlington'),
    ('Walter Reed Community Center', 'Arlington'),
    ('Arlington Central Library Auditorium', 'Arlington'),
    ('Arlington Central Library', 'Arlington'),
    ('Shirlington Library Plaza', 'Arlington'),
    ('Bennett Park Arts Atrium', 'Arlington'),
    ('Bennett Park Atrium', 'Arlington'),
    ('Ballston Quarter', 'Arlington'),
    ('Central United Methodist Church', 'Arlington'),
    ("Ireland's Four Provinces restaurant", 'Falls Church'),
)


def clean_text(value):
    value = value.replace('\xa0', ' ').replace('\u200b', ' ')
    return re.sub(r'\s+', ' ', value).strip(' |,-')


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def find_location(value):
    normalized = value.lower().replace('cent ', 'center ')
    for venue, city in VENUES:
        if venue.lower() in normalized:
            return venue, city
    return None


def event_title(prefix):
    prefix = re.sub(r'^.*\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b\s+', '', prefix, flags=re.I)
    prefix = re.sub(r'^(?:First|Second) performance (?:on )?', '', prefix, flags=re.I)
    prefix = re.sub(r'^(?:Concert and Fundraiser[^-]*-\s*)', '', prefix, flags=re.I)
    prefix = re.sub(r'\s+(?:First|Second) performance.*$', '', prefix, flags=re.I)
    prefix = clean_text(prefix)
    return prefix[-180:].strip(' ,-')


def normalize_title(title, context):
    title_lower = title.lower()
    lower = context.lower()
    if 'an evening of music and magic' in title_lower or (
        'tickets required' in title_lower and 'an evening of music and magic' in lower
    ):
        return 'An Evening of Music and Magic'
    if 'brassy brilliance' in title_lower or (
        title[:1].isdigit() and 'brassy brilliance' in lower
    ):
        return 'Holiday Brass Ensemble Concert: Brassy Brilliance'
    if 'great romantics' in title_lower:
        return 'Full Orchestra Concert: March Magic: Great Romantics'
    if title_lower == 'concert' and 'music in bloom' in lower:
        return 'Music in Bloom'
    if 'pops in the park' in lower and title.lower() in {'lubber run amphitheatre', 'concert'}:
        return 'Pops in the Park'
    return re.sub(r'\s+Two performances.*$', '', title, flags=re.I).strip(' ,-')


def parse_season(text, start_year, end_year):
    pieces = [clean_text(piece) for piece in text.split('|') if clean_text(piece)]
    records = []
    fall_piece_indexes = [
        index for index, piece in enumerate(pieces)
        if re.search(r'\b(?:August|September|October|November|December)\b', piece, re.I)
    ]
    first_fall_index = min(fall_piece_indexes, default=0)

    for index, piece in enumerate(pieces):
        matches = list(DATE_RE.finditer(piece))
        for match in matches:
            context = ' | '.join(pieces[max(0, index - 3):min(len(pieces), index + 4)])
            location = find_location(piece)
            if not location and 'performance' in piece.lower():
                location = find_location(' | '.join(pieces[max(0, index - 2):index]))
            if not location:
                continue

            before = piece[:match.start()]
            title = event_title(before)
            if not title:
                prior = ' '.join(pieces[max(0, index - 3):index])
                title = event_title(prior)
            title = normalize_title(title, context)

            lower_context = context.lower()
            if not title or 'virtual video' in lower_context:
                continue
            if 'art show and sale' in title.lower() or title.lower() == 'art show opening':
                continue

            month = MONTHS[match.group(1).lower()]
            if match.group(3):
                year = int(match.group(3))
            elif month >= 8:
                year = start_year
            else:
                year = start_year if index < first_fall_index else end_year
            try:
                event_date = date(year, month, int(match.group(2))).isoformat()
            except ValueError:
                continue

            venue, city = location
            records.append({
                'title': title,
                'date': event_date,
                'url': CALENDAR_URL,
                'time_from': parse_time(piece[match.end():]) or parse_time(piece),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': context,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    return records


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('.wsite-section-elements')
    if content is None:
        return []

    text = content.get_text(' | ', strip=True)
    seasons = list(SEASON_RE.finditer(text))
    records = []
    for index, match in enumerate(seasons):
        end = seasons[index + 1].start() if index + 1 < len(seasons) else len(text)
        records.extend(parse_season(text[match.end():end], int(match.group(1)), int(match.group(2))))

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class ArlingtonPhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='arlingtonphilharmonic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Arlington Philharmonic concert calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_calendar(response.text)
        if not records:
            log_message(
                'No Arlington Philharmonic concerts found',
                event='crawler_empty_result',
                level='warning',
                url=CALENDAR_URL,
                record_count=0,
            )
        return records


def main():
    ArlingtonPhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
