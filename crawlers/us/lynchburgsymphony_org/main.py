import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lynchburgsymphony.org/'
LISTING_URL = f'{SOURCE_URL}current-season/'
SOURCE = 'Lynchburg Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June', 'July',
         'August', 'September', 'October', 'November', 'December'),
        start=1,
    )
}
MONTH_PATTERN = '|'.join(MONTHS)
DATE_LINE_RE = re.compile(rf'\b(?:{MONTH_PATTERN})\b\s+\d{{1,2}}', re.I)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(?:p\.?m\.?|a\.?m\.?)', re.I)


def clean_text(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def parse_time_values(value):
    values = []
    for match in TIME_RE.finditer(value):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        marker = re.sub(r'[^ap]', '', match.group(0).lower())[-1:]
        if marker == 'p' and hour != 12:
            hour += 12
        elif marker == 'a' and hour == 12:
            hour = 0
        result = f'{hour:02d}:{minute:02d}'
        if result not in values:
            values.append(result)
    return values


def parse_dates(value, fallback_year=None):
    year_match = re.search(r'\b(20\d{2})\b', value)
    year = int(year_match.group(1)) if year_match else fallback_year
    if not year:
        return []

    dates = []
    current_month = None
    token_re = re.compile(rf'\b({MONTH_PATTERN})\b|\b(\d{{1,2}})(?:st|nd|rd|th)?\*?', re.I)
    date_part = re.split(r'\b20\d{2}\b', value, maxsplit=1)[0]
    date_part = re.split(r'(?:@|\bat\b|,)?\s+\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m\.?', date_part, maxsplit=1, flags=re.I)[0]
    for match in token_re.finditer(date_part):
        if match.group(1):
            current_month = MONTHS[match.group(1).title()]
            continue
        if current_month is None:
            continue
        day = int(match.group(2))
        try:
            parsed = datetime(year, current_month, day).date().isoformat()
        except ValueError:
            continue
        if parsed not in dates:
            dates.append(parsed)
    return dates


def block_lines(block):
    return [
        clean_text(line)
        for line in block.get_text('\n', strip=True).splitlines()
        if clean_text(line)
    ]


def title_before_date(lines, date_index):
    parts = [line for line in lines[:date_index] if line not in {'Upcoming Performances', 'Recent Performances'}]
    return clean_text(' '.join(parts))


def venue_and_city(lines, date_index):
    candidates = lines[date_index + 1:date_index + 5]
    venue = ''
    for line in candidates:
        if line in {'*', 'matinee performance'}:
            continue
        special = re.match(r'SPECIAL ENGAGEMENT at (.+?)(?:,\s*\d|$)', line, re.I)
        if special:
            venue = clean_text(special.group(1))
            break
        if re.search(r'\b(?:Theater|Theatre|Church|Hall|Ballroom|Club|Hotel|Campus|Festival|Forest|Mont)\b', line, re.I):
            venue = re.split(r',\s*(?:\d|Lynchburg\b|Orkney Springs\b)', line, maxsplit=1)[0].strip(' ,')
            break
    if not venue:
        return '', ''

    nearby = ' '.join(candidates)
    if re.search(r'Orkney Springs', nearby, re.I):
        city = 'Orkney Springs'
    elif re.search(r'Poplar Forest', venue, re.I):
        city = 'Forest'
    elif venue == 'Shenandoah Valley Music Festival':
        return '', ''
    else:
        city = 'Lynchburg'
    return venue, city


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    fallback_year = None
    for block in soup.select('.et_pb_text_inner'):
        lines = block_lines(block)
        date_index = next((i for i, line in enumerate(lines) if DATE_LINE_RE.search(line)), None)
        if date_index is None:
            continue
        date_line = lines[date_index]
        explicit_year = re.search(r'\b(20\d{2})\b', date_line)
        if explicit_year:
            fallback_year = int(explicit_year.group(1))
        dates = parse_dates(date_line, fallback_year)
        title = title_before_date(lines, date_index)
        venue, city = venue_and_city(lines, date_index)
        if not title or not dates or not venue or not city:
            continue

        times = parse_time_values(date_line)
        # A time range describes one occurrence, while "and/or" denotes multiple shows.
        if re.search(r'\d\s*(?:-|–)\s*\d', date_line):
            times = times[:1]
        if len(dates) > 1:
            times = [None]
        elif not times:
            times = [None]

        description = '\n\n'.join(lines[date_index + 2:]) or None
        for event_date in dates:
            for time_from in times:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': LISTING_URL,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class LynchburgSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lynchburgsymphony_org',
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
        return scrape_concerts()


def main():
    LynchburgSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
