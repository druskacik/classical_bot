import html
import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hartfordsymphony.org/'
SOURCE = 'Hartford Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/avada_portfolio'

# First-party portfolio categories used for concerts and concert archives.
CATEGORY_IDS = '70,71,72,78,80,81,83,91,92'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Referer': f'{SOURCE_URL}events/concerts/',
}

VENUE_CITIES = {
    'Belding Theater at The Bushnell': 'Hartford',
    'Mortensen Hall at The Bushnell': 'Hartford',
    'Wadsworth Atheneum Museum of Art': 'Hartford',
    'J Under the Dome': 'Hartford',
    'Trinity College Chapel': 'Hartford',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RANGE_RE = re.compile(
    rf'\b({MONTHS})\s+(\d{{1,2}})\s*[-–]\s*'
    rf'(?:({MONTHS})\s+)?'
    r'(\d{1,2}),\s*(\d{4})\b',
    re.I,
)
DATE_PAIR_RE = re.compile(
    rf'\b({MONTHS})\s+(\d{{1,2}})\s*(?:&|and)\s*(\d{{1,2}}),\s*(\d{{4}})\b',
    re.I,
)
DATE_SINGLE_RE = re.compile(rf'\b({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})\b', re.I)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?(?:\b|$)', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_date(month, day, year):
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date()
    except ValueError:
        return None


def parse_dates(value):
    value = clean_text(value)
    match = DATE_RANGE_RE.search(value)
    if match:
        month_one, day_one, month_two, day_two, year = match.groups()
        start = make_date(month_one, day_one, year)
        end = make_date(month_two or month_one, day_two, year)
        if start and end and end >= start and (end - start).days <= 7:
            return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]

    match = DATE_PAIR_RE.search(value)
    if match:
        month, day_one, day_two, year = match.groups()
        return [item for item in (make_date(month, day_one, year), make_date(month, day_two, year)) if item]

    match = DATE_SINGLE_RE.search(value)
    if match:
        parsed = make_date(*match.groups())
        return [parsed] if parsed else []
    return []


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def times_for_dates(lines, dates):
    times = {}
    for line in lines:
        for weekday in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'):
            if weekday.lower() in line.lower():
                parsed_time = parse_time(line)
                if parsed_time:
                    for event_date in dates:
                        if event_date.strftime('%A') == weekday:
                            times[event_date] = parsed_time

    # Some pages combine two weekdays before one time, e.g. "Friday & Saturday at 8 PM".
    for line in lines:
        parsed_time = parse_time(line)
        if not parsed_time:
            continue
        for event_date in dates:
            if event_date.strftime('%A').lower() in line.lower():
                times[event_date] = parsed_time

    # Nutcracker-style lines contain the date and multiple performances.
    occurrences = []
    for event_date in dates:
        dated_line = next(
            (line for line in lines if re.search(rf'\b{event_date.strftime("%B")}\s+{event_date.day}\b', line, re.I)),
            '',
        )
        line_times = [parse_time(match.group(0)) for match in TIME_RE.finditer(dated_line)]
        line_times = [item for item in line_times if item]
        if line_times:
            occurrences.extend((event_date, item) for item in line_times)
        else:
            occurrences.append((event_date, times.get(event_date)))

    if len(dates) == 1 and occurrences and occurrences[0][1] is None:
        # Ignore gallery/pre-concert times when the line explicitly labels the concert time.
        concert_line = next((line for line in lines if 'concert' in line.lower() and parse_time(line)), '')
        concert_times = [parse_time(match.group(0)) for match in TIME_RE.finditer(concert_line)]
        fallback = concert_times[-1] if concert_times else None
        if not fallback:
            fallback = next((parse_time(line) for line in lines if parse_time(line)), None)
        occurrences[0] = (dates[0], fallback)
    return occurrences


def event_records(item):
    title = clean_text(item.get('title', {}).get('rendered'))
    url = item.get('link', '').strip()
    description = clean_text(item.get('content', {}).get('rendered'))
    meta_description = clean_text(item.get('aioseo_head_json', {}).get('description'))
    dates = parse_dates(meta_description or description)
    lines = [line.strip() for line in description.splitlines() if line.strip()]

    venue = next((name for name in VENUE_CITIES if name.lower() in description.lower()), '')
    if not title or not url or not dates or not venue:
        return []
    if 'not open to the public' in description.lower():
        return []

    return [
        {
            'title': title,
            'date': event_date.isoformat(),
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': VENUE_CITIES[venue],
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in times_for_dates(lines, dates)
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'portfolio_category': CATEGORY_IDS,
                'per_page': 100,
                'page': page,
                '_fields': 'link,title,content,aioseo_head_json',
            },
            timeout=60,
        )
        response.raise_for_status()
        items = response.json()
        for item in items:
            records.extend(event_records(item))

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No parseable concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class HartfordSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hartfordsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    HartfordSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
