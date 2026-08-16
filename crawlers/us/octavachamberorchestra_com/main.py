import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://octavachamberorchestra.com/'
# The site's HTTPS certificate currently names a different host. Its first-party
# HTTP endpoint serves the same pages and is the usable transport for scraping.
FETCH_URL = 'http://octavachamberorchestra.com/'
SEASON_URL = f'{FETCH_URL}season.shtml'
SOURCE = 'The Octava Chamber Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'jan': 1,
    'january': 1,
    'feb': 2,
    'february': 2,
    'mar': 3,
    'march': 3,
    'apr': 4,
    'april': 4,
    'may': 5,
    'jun': 6,
    'june': 6,
    'jul': 7,
    'july': 7,
    'aug': 8,
    'august': 8,
    'sep': 9,
    'sept': 9,
    'september': 9,
    'oct': 10,
    'october': 10,
    'nov': 11,
    'november': 11,
    'dec': 12,
    'december': 12,
}

LOCATIONS = {
    'maple park church': ('Maple Park Church', 'Lynnwood'),
}

DATE_PATTERN = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+'
    r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)$',
    re.IGNORECASE,
)
SEASON_PATTERN = re.compile(r'\b(20\d{2})\s*[-–]\s*(20\d{2})\s+Concert Season\b', re.I)


def clean_text(value):
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date_time(value, start_year, end_year):
    match = DATE_PATTERN.match(clean_text(value))
    if not match:
        return None, None
    month = MONTHS.get(match.group(1).lower())
    if month is None:
        return None, None
    year = start_year if month >= 7 else end_year
    try:
        event_date = date(year, month, int(match.group(2))).isoformat()
    except ValueError:
        return None, None

    hour = int(match.group(3)) % 12
    if match.group(5).lower() == 'pm':
        hour += 12
    minute = int(match.group(4) or '00')
    if minute > 59:
        return None, None
    return event_date, f'{hour:02d}:{minute:02d}'


def parse_location(value):
    location = re.sub(r'^at\s+', '', clean_text(value), flags=re.I).strip(' ,')
    return LOCATIONS.get(location.lower())


def season_lines(soup):
    for paragraph in soup.find_all('p'):
        lines = [clean_text(line) for line in paragraph.get_text('\n', strip=True).splitlines()]
        if any(SEASON_PATTERN.search(line) for line in lines):
            return [line for line in lines if line]
    return []


def parse_season(html):
    soup = BeautifulSoup(html, 'html.parser')
    lines = season_lines(soup)
    season_index = next(
        (index for index, line in enumerate(lines) if SEASON_PATTERN.search(line)),
        None,
    )
    if season_index is None:
        return []

    season_match = SEASON_PATTERN.search(lines[season_index])
    start_year, end_year = map(int, season_match.groups())
    starts = [
        index for index in range(season_index + 1, len(lines))
        if DATE_PATTERN.match(lines[index])
    ]
    records = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        event_date, time_from = parse_date_time(lines[start], start_year, end_year)
        location = parse_location(lines[start + 1]) if start + 1 < end else None
        if not event_date or not location:
            continue

        description_lines = [
            line for line in lines[start + 2:end]
            if line.lower() != 'all programs subject to change'
        ]
        description = '\n'.join(description_lines) or None
        venue, city = location
        records.append({
            'title': 'Octava Chamber Orchestra Concert',
            'date': event_date,
            'url': SEASON_URL,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class OctavaChamberOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='octavachamberorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(SEASON_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Octava concert season',
                event='crawler_fetch_failed',
                level='error',
                url=SEASON_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_season(response.content)
        if not records:
            log_message(
                'No complete Octava concerts found',
                event='crawler_empty_result',
                level='warning',
                url=SEASON_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or ''))


def main():
    OctavaChamberOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
