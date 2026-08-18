import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chintimini.org/'
SOURCE = 'Chintimini Chamber Music Festival'
CITY = 'Corvallis'
SEASON_URL = f'{SOURCE_URL}2026-season'
ARCHIVE_URL = f'{SOURCE_URL}past-festivals'
PAGE_URLS = (SEASON_URL, ARCHIVE_URL)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{2,4})\s+at\s+'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)'
    r'(?P<remainder>.*)',
    re.IGNORECASE,
)
TITLE_RE = re.compile(r'(?:concert\s+no\.\s*\d+|.+\s+-\s+concert\s+no\.\s*\d+)', re.I)
IGNORE_LINES = {
    'directions',
    'tickets available at the door',
    'join the mailing list!',
    'name',
    'first name',
    'last name',
    'email',
    'submit',
}


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'[ \t]+', ' ', text).strip()


def page_lines(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []
    return [clean_text(line) for line in main.get_text('\n').splitlines() if clean_text(line)]


def parse_date(match):
    year = int(match.group('year'))
    if year < 100:
        year += 2000
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    compact = re.sub(r'\s+', '', value).upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(compact, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def title_before(lines, date_index):
    for line in reversed(lines[max(0, date_index - 4):date_index]):
        if TITLE_RE.search(line):
            return line
    return ''


def venue_for(lines, date_index, match):
    remainder = match.group('remainder')
    # Archive entries append the venue after a comma; one has a duplicated time.
    comma_parts = [clean_text(part) for part in remainder.split(',') if clean_text(part)]
    if comma_parts:
        venue = re.sub(r'^(?:at\s+)?\d{1,2}(?::\d{2})?\s*[ap]m\s*,?\s*', '', comma_parts[-1], flags=re.I)
        venue = re.sub(r'^at\s+', '', venue, flags=re.I)
        if venue and not re.fullmatch(r'\d{1,2}(?::\d{2})?\s*[ap]m', venue, re.I):
            return venue

    if date_index + 1 < len(lines):
        candidate = lines[date_index + 1]
        if not DATE_RE.search(candidate) and not TITLE_RE.search(candidate):
            return candidate
    return ''


def records_from_html(html, url):
    lines = page_lines(html)
    date_indexes = [index for index, line in enumerate(lines) if DATE_RE.search(line)]
    records = []
    for position, index in enumerate(date_indexes):
        match = DATE_RE.search(lines[index])
        title = title_before(lines, index)
        event_date = parse_date(match)
        venue = venue_for(lines, index, match)
        if not title or not event_date or not venue:
            continue

        end = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        description_lines = []
        for line in lines[index + 1:end]:
            if line == venue or TITLE_RE.search(line) or line.lower() in IGNORE_LINES:
                continue
            if line.lower().startswith(('tickets available', 'download the ')):
                continue
            description_lines.append(line)

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(match.group('time')),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': '\n'.join(description_lines) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in PAGE_URLS:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            page_records = records_from_html(response.text, url)
            records.extend(page_records)
            log_message(
                'Concert page parsed',
                event='crawler_page_parsed',
                url=url,
                record_count=len(page_records),
            )
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_failed',
                level='error',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ChintiminiOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chintimini_org',
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
    ChintiminiOrgCrawler().run()


if __name__ == '__main__':
    main()
