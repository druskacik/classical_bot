import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.westchesterchambersoloists.com/'
SOURCE = 'Westchester Chamber Soloists'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

MONTH_NUMBER_BY_PREFIX = {name[:3]: number for name, number in MONTHS.items()}

SEASON_RE = re.compile(r'\b(20\d{2})\s*[-–]\s*(\d{2,4})\s+SEASON\b', re.IGNORECASE)
DATE_RE = re.compile(
    r'^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,\s*)?'
    r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|'
    r'Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\s+(\d{1,2})(?:\s*&\s*(\d{1,2}))?(?:\s*,\s*(20\d{2}))?\b',
    re.IGNORECASE,
)
NEW_YEAR_RE = re.compile(r"^New Year['’]s Day\s*&\s*January\s+(\d{1,2}),\s*(20\d{2})$", re.I)
TIME_RE = re.compile(r'\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?\b', re.IGNORECASE)


def clean_line(value):
    value = value.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    value = value.replace('\u200d', '')
    return re.sub(r'\s+', ' ', value).strip()


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1))
    if not 1 <= hour <= 12:
        return None
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == 'p' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'a' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def year_for_season(month, start_year, end_year):
    return start_year if month >= 7 else end_year


def parse_location(text):
    lowered = text.lower()
    if 'purchase performing arts center' in lowered:
        return 'Purchase Performing Arts Center', 'Purchase'
    if 'sarah lawrence college' in lowered or 'reisinger hall' in lowered:
        return 'Reisinger Hall at Sarah Lawrence College', 'Bronxville'

    # WCS is Sarah Lawrence College's resident orchestra. The page explicitly
    # identifies Purchase dates; its remaining home-season concerts are at
    # Reisinger Hall, including entries whose individual listing omits a venue.
    return 'Reisinger Hall at Sarah Lawrence College', 'Bronxville'


def parse_page(html, page_url=SOURCE_URL, default_season=None):
    soup = BeautifulSoup(html, 'html.parser')
    lines = []
    for block in soup.select('main .html-block .sqs-html-content'):
        block_text = clean_line(block.get_text(' ', strip=True))
        if 'concerts):' in block_text and 'Season Subscriptions' in block_text:
            # This is the introductory slash-separated season date summary,
            # not an event listing. The concrete entries occur in later blocks.
            continue
        lines.extend(
            line for line in (clean_line(value) for value in block.get_text('\n').splitlines())
            if line
        )

    records = []
    season = default_season
    current = None
    pending_prefix = []

    def finish_event():
        nonlocal current
        if current is None:
            return
        description = '\n'.join(current.pop('description_lines')).strip() or None
        venue, city = parse_location(description or '')
        event_dates = current.pop('event_dates')
        for event_date in event_dates:
            record = dict(current)
            record.update(
                title=f'{SOURCE} – {event_date.strftime("%B %-d, %Y")}',
                date=event_date.isoformat(),
                url=page_url,
                venue=venue,
                city=city,
                country_code='US',
                description=description,
                source_url=SOURCE_URL,
                source=SOURCE,
            )
            records.append(record)
        current = None

    for line in lines:
        season_match = SEASON_RE.search(line)
        if season_match:
            finish_event()
            start_year = int(season_match.group(1))
            raw_end = season_match.group(2)
            end_year = int(raw_end) if len(raw_end) == 4 else (start_year // 100) * 100 + int(raw_end)
            season = (start_year, end_year)
            continue

        if re.match(r'^WCS\s+(?:performs|presents|at)\b', line, re.I) or line == 'The Brandenburg Concertos':
            pending_prefix.append(line)
            continue

        date_match = DATE_RE.match(line)
        new_year_match = NEW_YEAR_RE.match(line)
        if (not date_match and not new_year_match) or season is None:
            if current is not None:
                current['description_lines'].append(line)
            continue

        finish_event()
        if new_year_match:
            year = int(new_year_match.group(2))
            date_parts = [(1, 1), (1, int(new_year_match.group(1)))]
        else:
            month = MONTH_NUMBER_BY_PREFIX[date_match.group(1).lower()[:3]]
            year = int(date_match.group(4)) if date_match.group(4) else year_for_season(month, *season)
            date_parts = [(month, int(date_match.group(2)))]
            if date_match.group(3):
                date_parts.append((month, int(date_match.group(3))))
        try:
            event_dates = [date(year, month, day) for month, day in date_parts]
        except ValueError:
            pending_prefix = []
            continue

        current = {
            'event_dates': event_dates,
            'time_from': parse_time(line),
            'description_lines': [*pending_prefix, line],
        }
        pending_prefix = []

    finish_event()
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class WestchesterChamberSoloistsComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='westchesterchambersoloists_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        pages = [
            (SOURCE_URL, None),
            (f'{SOURCE_URL}performances-2', (2023, 2024)),
            (f'{SOURCE_URL}performances', (2022, 2023)),
        ]
        records = []
        session = requests.Session()
        session.headers.update(HEADERS)
        for page_url, default_season in pages:
            try:
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Westchester Chamber Soloists performances',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            records.extend(parse_page(response.text, page_url, default_season))

        records.sort(key=lambda record: (record['date'], record['time_from'] or '', record['title']))
        if not records:
            log_message(
                'No Westchester Chamber Soloists performances found',
                event='crawler_empty_result',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return records


def main():
    WestchesterChamberSoloistsComCrawler().run()


if __name__ == '__main__':
    main()
