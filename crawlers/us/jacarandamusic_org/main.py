import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jacarandamusic.org/'
SOURCE = 'Jacaranda Music'
ARCHIVE_URL = f'{SOURCE_URL}concerts'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}
MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(
    rf'\b(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:,|\s)', re.I
)
YEAR_RE = re.compile(r'\b(20\d{2})\b')
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.I)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        # Older archive entries use bare 24-hour-looking values such as 8:00.
        match = re.search(r'\b(\d{1,2}):(\d{2})\b', value or '')
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 8:
            hour += 12
        return f'{hour:02d}:{minute:02d}'
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = match.group(3).lower()
    if meridiem == 'pm' and hour != 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_dates(date_line, season_start_year=None):
    match = DATE_RE.search(date_line)
    if not match:
        return []
    explicit_year = YEAR_RE.search(date_line)
    month = datetime.strptime(match.group('month').title(), '%B').month
    year = int(explicit_year.group(1)) if explicit_year else None
    if year is None and season_start_year:
        year = season_start_year if month >= 7 else season_start_year + 1
    if year is None:
        return []

    days = [int(match.group('day'))]
    paired_days = re.search(
        rf'\b{match.group("month")}\s+(\d{{1,2}})\s*&\s*(\d{{1,2}})',
        date_line,
        re.I,
    )
    if paired_days:
        days = [int(paired_days.group(1)), int(paired_days.group(2))]

    dates = []
    for day in days:
        try:
            dates.append(datetime(year, month, day).date().isoformat())
        except ValueError:
            pass
    return dates


def location_for(lines):
    opening = ' '.join(lines[:8])
    if 'Villa Aurora' in opening:
        return 'Villa Aurora', 'Los Angeles'
    if 'UCLA Schoenberg Hall' in opening:
        return 'UCLA Schoenberg Hall', 'Los Angeles'
    if 'Kirk Douglas Theatre' in opening:
        return 'Kirk Douglas Theatre', 'Culver City'
    if 'Walt Disney Concert Hall' in opening:
        return 'Walt Disney Concert Hall', 'Los Angeles'
    if 'Barnum Hall' in opening:
        return 'Barnum Hall', 'Santa Monica'
    if 'Zipper Hall' in opening:
        return 'Zipper Hall', 'Los Angeles'
    return 'First Presbyterian Church of Santa Monica', 'Santa Monica'


def title_for(lines):
    ignored = {
        'SPECIAL EVENT', 'SANCTUARY SERIES', 'PRE-CONCERT CONVERSATIONS',
    }
    venue_terms = (
        'Church', 'Hall', 'Theatre', 'Villa Aurora', 'Shuttle Service',
        'Washington Boulevard', 'Paseo Miramar', 'Charles E Young',
        'Noon to Midnight/',
    )
    for line in lines[1:10]:
        candidate = clean_text(line).strip(' –-')
        if not candidate or candidate in ignored:
            continue
        if any(term in candidate for term in venue_terms):
            continue
        if re.match(r'^\d+\s+', candidate):
            continue
        # Programme titles on this page are consistently set in capitals.
        letters = [char for char in candidate if char.isalpha()]
        if letters and sum(char.isupper() for char in letters) / len(letters) >= 0.7:
            return candidate
    return ''


def archive_records(html):
    soup = BeautifulSoup(html, 'html.parser')
    blocks = soup.select('.sqs-html-content')
    records = []
    season_start_year = None
    index = 0
    while index < len(blocks):
        text = clean_text(blocks[index].get_text('\n', strip=True))
        season = re.search(r'\b(20\d{2})-(?:\d{2}|20\d{2})\b', text)
        if season and text.startswith('PAST'):
            season_start_year = int(season.group(1))

        lines = text.splitlines()
        if lines and DATE_RE.search(lines[0]):
            # On two special events the date, label, venue and programme are
            # separate Squarespace text blocks.
            if len(text) < 100 and index + 2 < len(blocks):
                additions = [
                    clean_text(blocks[index + offset].get_text('\n', strip=True))
                    for offset in (1, 2)
                ]
                text = clean_text('\n'.join([text, *additions]))
                lines = text.splitlines()

            # These short blocks advertise talks before the actual concerts.
            if 'SPECIAL EVENT' not in text and 'PRE-CONCERT CONVERSATIONS' not in text and not any(
                line.startswith('Sanctuary,') for line in lines[:5]
            ):
                dates = parse_dates(lines[0], season_start_year)
                title = title_for(lines)
                venue, city = location_for(lines)
                for event_date in dates:
                    if title:
                        records.append({
                            'title': title,
                            'date': event_date,
                            'url': ARCHIVE_URL,
                            'time_from': parse_time(lines[0]),
                            'venue': venue,
                            'city': city,
                            'description': text,
                            'source_url': SOURCE_URL,
                            'source': SOURCE,
                        })
        index += 1
    return records


def homepage_records(html):
    text = clean_text(BeautifulSoup(html, 'html.parser').get_text('\n', strip=True))
    pattern = re.compile(
        rf'(?P<title>[^\n]+)\n(?P<month>{MONTHS})\s+'
        r'(?P<day>\d{1,2}),\s*(?P<year>20\d{2})\s*-\s*(?P<venue>[^\n]+)',
        re.I,
    )
    records = []
    for match in pattern.finditer(text):
        date_text = f'{match.group("month")} {match.group("day")}, {match.group("year")}'
        dates = parse_dates(date_text)
        venue, city = location_for([match.group('venue')])
        if dates and venue:
            records.append({
                'title': clean_text(match.group('title')),
                'date': dates[0],
                'url': SOURCE_URL,
                'time_from': None,
                'venue': venue,
                'city': city,
                'description': None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class JacarandaMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jacarandamusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for url, parser in (
            (SOURCE_URL, homepage_records),
            (ARCHIVE_URL, archive_records),
        ):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parser(response.text))
            except requests.RequestException as error:
                log_message(
                    'Jacaranda page request failed',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    JacarandaMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
