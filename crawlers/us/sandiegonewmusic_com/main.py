import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sandiegonewmusic.com/'
EVENTS_URL = f'{SOURCE_URL}events.html'
ARCHIVE_URL = f'{SOURCE_URL}archive.html'
SOURCE = 'San Diego New Music'
DEFAULT_VENUE = 'Athenaeum Music & Arts Library'
DEFAULT_CITY = 'San Diego'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month.lower(): number for number, month in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    ) if month
}

DATE_RE = re.compile(
    r'^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
    r'(?:\s*[-–]\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?,?\s+)?'
    r'(?P<month>[A-Za-z]+)\s+(?P<start>\d{1,2})'
    r'(?:\s*[-–]\s*(?P<end>\d{1,2}))?'
    r'(?:,\s*(?P<year>20\d{2}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*'
    r'(?P<period>[ap])\.?\s*m\.?', re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group('hour')) % 12
    if match.group('period').lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group("minute") or "00"}'


def parse_dates(text, fallback_year=None):
    match = DATE_RE.match(text.strip())
    if not match:
        return []
    month = MONTHS.get(match.group('month').lower())
    year = int(match.group('year') or fallback_year or 0)
    if not month or not year:
        return []
    try:
        first = date(year, month, int(match.group('start')))
        last = date(year, month, int(match.group('end') or match.group('start')))
    except ValueError:
        return []
    if last < first or (last - first).days > 14:
        return []
    return [(first + timedelta(days=offset)).isoformat()
            for offset in range((last - first).days + 1)]


def title_after_date(text):
    text = DATE_RE.sub('', text.strip(), count=1)
    text = TIME_RE.sub('', text, count=1)
    text = re.sub(r'\bnightly\s+at\s*(?=[—–-])', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^[\s,;:—–-]+', '', text)
    lines = [line.strip(' —–-,;:') for line in clean_text(text).splitlines()]
    lines = [line for line in lines if line]
    if len(lines) > 1 and lines[0].endswith("'s"):
        return f'{lines[0]} {lines[1]}'
    return lines[0] if lines else ''


TOUR_LOCATIONS = (
    ('adelphi university', 'Adelphi University', 'Garden City', 'US'),
    ('university of california san diego', 'University of California San Diego', 'San Diego', 'US'),
    ('university of wisconsin-milwaukee', 'University of Wisconsin-Milwaukee', 'Milwaukee', 'US'),
    ('university of california santa cruz', 'University of California Santa Cruz', 'Santa Cruz', 'US'),
    ('stanford university', 'Stanford University', 'Stanford', 'US'),
    ('california institute of the arts', 'California Institute of the Arts', 'Valencia', 'US'),
    ('university of san diego', 'University of San Diego', 'San Diego', 'US'),
    ('university of virginia', 'University of Virginia', 'Charlottesville', 'US'),
)


def location_from_text(text):
    lower = text.lower()
    for needle, venue, city, country_code in TOUR_LOCATIONS:
        if needle in lower:
            return venue, city, country_code
    # A touring entry must not inherit the presenter's San Diego defaults.
    if re.search(r'\b(?:in residence|performance)\s+(?:at|on)\b', lower):
        return None
    if 'athenaeum art center' in lower or 'athenaeum arts center' in lower:
        return 'Athenaeum Art Center at Bread & Salt', DEFAULT_CITY, 'US'
    if 'athenaeum music & arts library' in lower or 'the athenaeum music and arts library' in lower:
        return 'Athenaeum Music & Arts Library', DEFAULT_CITY, 'US'
    return DEFAULT_VENUE, DEFAULT_CITY, 'US'


def make_records(title, dates, url, text, time_from=None):
    if not title or not dates:
        return []
    location = location_from_text(text)
    if not location:
        return []
    venue, city, country_code = location
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in dates]


def current_records(soup):
    season = soup.find(string=re.compile(r'20\d{2}\s*[-–]\s*20\d{2}\s+CONCERT SEASON', re.I))
    if not season:
        return []
    years = [int(value) for value in re.findall(r'20\d{2}', str(season))]
    records = []
    for heading in soup.select('h2'):
        heading_text = clean_text(heading)
        match = DATE_RE.match(heading_text)
        if not match:
            continue
        month = MONTHS.get(match.group('month').lower())
        fallback_year = years[0] if month and month >= 7 else years[-1]
        title = title_after_date(heading_text)
        description_node = heading.find_next_sibling(['p', 'blockquote'])
        description = clean_text(description_node)
        details = '\n'.join(value for value in (heading_text, description) if value)
        records.extend(make_records(
            title, parse_dates(heading_text, fallback_year), EVENTS_URL,
            details, parse_time(heading_text),
        ))
    return records


def archive_records(soup):
    marker = soup.find(string=re.compile(r'ARCHIVE OF PAST EVENTS', re.I))
    container = marker.find_parent('td') if marker else None
    if not container:
        return []

    segments = []
    current = None
    for node in container.children:
        if not isinstance(node, Tag):
            continue
        text = clean_text(node)
        if not text:
            continue
        first_line = text.split('\n', 1)[0]
        dates = parse_dates(first_line)
        if dates:
            if current:
                segments.append(current)
            current = {'dates': dates, 'nodes': [node]}
        elif current:
            current['nodes'].append(node)
    if current:
        segments.append(current)

    records = []
    for segment in segments:
        texts = [clean_text(node) for node in segment['nodes']]
        description = '\n\n'.join(value for value in texts if value)
        if re.search(r'\((?:video premiere|livestream event)\)', description, re.IGNORECASE):
            continue
        first_text = texts[0]
        title = title_after_date(first_text)
        if not title:
            for line in description.splitlines()[1:]:
                candidate = line.strip()
                if (candidate and not TIME_RE.search(candidate)
                        and 'athenaeum' not in candidate.lower()
                        and not candidate.lower().startswith(('curated by', 'performed by'))):
                    title = candidate
                    break
        records.extend(make_records(
            title, segment['dates'], ARCHIVE_URL, description, parse_time(first_text)
        ))
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url, parser in ((EVENTS_URL, current_records), (ARCHIVE_URL, archive_records)):
        try:
            records.extend(parser(get_soup(session, url)))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape San Diego New Music page',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    unique = {(record['title'], record['date'], record['venue']): record for record in records}
    return sorted(unique.values(), key=lambda record: (record['date'], record['title']))


class SanDiegoNewMusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sandiegonewmusic_com',
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
        return get_concerts()


def main():
    SanDiegoNewMusicComCrawler().run()


if __name__ == '__main__':
    main()
