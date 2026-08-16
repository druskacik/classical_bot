import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://clearlakesymphony.org/'
SOURCE = 'Clear Lake Symphony'
CURRENT_SEASON_URL = urljoin(SOURCE_URL, 'season-schedule-2/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'previous-season-concerts/')
DEFAULT_VENUE = 'Gloria Dei Lutheran Church Auditorium'
DEFAULT_CITY = 'Nassau Bay'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
FULL_DATE_RE = re.compile(
    rf'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    rf'({MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})',
    re.IGNORECASE,
)
SHARED_YEAR_RE = re.compile(
    rf'({MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:and|&)\s+'
    rf'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    rf'(?:(?:({MONTH})\s+)?)?(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?\s*M\.?', re.IGNORECASE)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip(' _\n')


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def extract_occurrences(value):
    """Return every concrete date in a date line, including shared-year ranges."""
    occurrences = []
    shared = SHARED_YEAR_RE.search(value)
    if shared:
        month_one, day_one, month_two, day_two, year = shared.groups()
        parts = [(month_one, day_one), (month_two or month_one, day_two)]
    else:
        parts = [match.groups() for match in FULL_DATE_RE.finditer(value)]

    times = [parse_time(match.group(0)) for match in TIME_RE.finditer(value)]

    for index, (month, day, year) in enumerate(
        [(part[0], part[1], shared.group(5)) for part in parts] if shared else parts
    ):
        try:
            date = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
        except ValueError:
            continue
        time_from = times[index] if index < len(times) else None
        occurrences.append((date, time_from))
    return occurrences


def season_urls(session):
    response = session.get(ARCHIVE_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = [CURRENT_SEASON_URL]
    for link in soup.select('.entry-content a[href]'):
        url = urljoin(ARCHIVE_URL, link.get('href'))
        if 'season-schedule' in url and url not in urls:
            urls.append(url)
    return urls


def content_lines(soup):
    container = soup.select_one('.entry-content')
    if not container:
        return []
    lines = []
    for node in container.find_all(['p', 'h1', 'h2', 'h3'], recursive=False):
        text = clean_text(node.get_text(' ', strip=True))
        if text and not re.search(r'(?:total views|return to top)', text, re.I):
            lines.append(text)
    return lines


def event_title(block, date_line):
    candidates = []
    for line in block:
        without_dates = FULL_DATE_RE.sub('', line)
        without_dates = TIME_RE.sub('', without_dates)
        without_dates = clean_text(re.sub(r'^[\s–—-]+|[\s–—-]+$', '', without_dates))
        if re.search(r'\b(concert|classics|pops)\b', without_dates, re.I):
            candidates.append(without_dates)
    if candidates:
        return min(candidates, key=len)
    return f'{SOURCE} Concert'


def parse_season_page(html, page_url):
    lines = content_lines(BeautifulSoup(html, 'html.parser'))
    date_indexes = [
        index for index, line in enumerate(lines)
        if extract_occurrences(line) and not line.lower().startswith('click here for instructions')
    ]
    records = []
    for position, start in enumerate(date_indexes):
        end = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        # Titles are sometimes placed immediately before the date on older pages.
        prefix = lines[start - 1:start] if start and not extract_occurrences(lines[start - 1]) else []
        block = prefix + lines[start:end]
        date_line = lines[start]
        if re.search(r'live[ -]?stream|on-line only|free on-line concert', ' '.join(block), re.I):
            continue
        title = event_title(block, date_line)
        description = '\n'.join(dict.fromkeys(block)) or None
        venue = DEFAULT_VENUE
        for line in block:
            match = re.search(r'Location:\s*(.+?)(?:\s+_{3,})?$', line, re.I)
            if match and clean_text(match.group(1)):
                venue = clean_text(match.group(1))
                venue = re.split(r'\s+[–—-]\s+(?:Live|FREE)\b', venue, maxsplit=1, flags=re.I)[0]
                if venue == 'Gloria Dei Lutheran Church':
                    venue = DEFAULT_VENUE
                break
        occurrences = extract_occurrences(date_line)
        block_times = [parse_time(match.group(0)) for match in TIME_RE.finditer(' '.join(block))]
        if len(occurrences) == 1 and len(block_times) > 1:
            occurrences = [(occurrences[0][0], value) for value in block_times]
        for occurrence_index, (date, time_from) in enumerate(occurrences):
            if not time_from and len(block_times) == len(occurrences):
                time_from = block_times[occurrence_index]
            records.append({
                'title': title,
                'date': date,
                'url': page_url,
                'time_from': time_from or '19:30',
                'venue': venue,
                'city': DEFAULT_CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page_url in season_urls(session):
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            records.extend(parse_season_page(response.text, page_url))
        except requests.RequestException as error:
            log_message(
                'Season page request failed',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['date'], record['time_from'], record['title'], record['venue'])
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))
    if not result:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CURRENT_SEASON_URL,
            record_count=0,
        )
    return result


class ClearLakeSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='clearlakesymphony_org',
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
    ClearLakeSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
