import calendar
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operasouthwest.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Opera Southwest'
CITY = 'Albuquerque'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = '|'.join(calendar.month_name[1:])
DATE_RE = re.compile(
    rf'(?:(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|'
    rf'Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.?,?\s+)?'
    rf'(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*'
    rf'(?P<year>20\d{{2}})?\s*'
    rf'(?P<times>(?:\d{{1,2}}(?::\d{{2}})?\s*[AP]M)'
    rf'(?:\s*(?:&|and)\s*\d{{1,2}}(?::\d{{2}})?\s*[AP]M)*)',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\d{1,2}(?::\d{2})?\s*[AP]M', re.IGNORECASE)

EXCLUDED_SLUGS = {
    'chorus-subscription',
    'new-mexico-symphonic-chorus',
    'season-subscription',
    'waiting-list-nmsc-requiem',
    'dinner-with-dolores',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    value = re.sub(r'\s+', ' ', value.strip().upper())
    value = re.sub(r'(?<=\d)(?=[AP]M$)', ' ', value)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def infer_year(match, page):
    if match.group('year'):
        return int(match.group('year'))

    published_year = int(page['date'][:4])
    weekday = match.group('weekday')
    if not weekday:
        return published_year

    for year in (published_year, published_year + 1, published_year - 1):
        try:
            value = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
            )
        except ValueError:
            continue
        if value.strftime('%a').lower() == weekday[:3].lower():
            return year
    return published_year


def extract_occurrences(description, page):
    matches = list(DATE_RE.finditer(description))
    explicit_years = [int(match.group('year')) for match in matches if match.group('year')]
    page_year = explicit_years[0] if explicit_years else None
    occurrences = []

    for match in matches:
        year = int(match.group('year')) if match.group('year') else page_year
        year = year or infer_year(match, page)
        try:
            event_date = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
            ).date().isoformat()
        except ValueError:
            continue

        for time_value in TIME_RE.findall(match.group('times')):
            parsed_time = parse_time(time_value)
            if parsed_time:
                occurrences.append((event_date, parsed_time))
    return occurrences


def extract_venue(description):
    compact = re.sub(r'\s+', ' ', description)
    if re.search(r'Albuquerque Museum Amphitheater', compact, re.IGNORECASE):
        return 'Albuquerque Museum Amphitheater'
    if re.search(
        r'National Hispanic Cultural Center(?: Albuquerque)? Journal Theat(?:re|er)',
        compact,
        re.IGNORECASE,
    ):
        return 'Albuquerque Journal Theatre at the National Hispanic Cultural Center'
    return None


def fetch_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page_number},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page_number >= total_pages:
            break
        page_number += 1
    return pages


def descendant_ids(pages, root_id=6):
    result = {root_id}
    changed = True
    while changed:
        changed = False
        for page in pages:
            if page.get('parent') in result and page['id'] not in result:
                result.add(page['id'])
                changed = True
    return result


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    pages = fetch_pages(session)
    in_scope_ids = descendant_ids(pages)
    records = []

    for page in pages:
        if page['id'] not in in_scope_ids or page.get('slug') in EXCLUDED_SLUGS:
            continue
        description = clean_text(page.get('content', {}).get('rendered'))
        venue = extract_venue(description)
        if not venue:
            continue
        title = clean_text(page.get('title', {}).get('rendered'))
        url = page.get('link', '').strip()
        if not title or not url:
            continue
        for event_date, time_from in extract_occurrences(description, page):
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(
        unique.values(), key=lambda item: (item['date'], item['time_from'], item['title'])
    )
    if not result:
        log_message(
            'No concrete Opera Southwest performances found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return result


class OperaSouthwestOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operasouthwest_org',
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
        return scrape_concerts()


def main():
    OperaSouthwestOrgCrawler().run()


if __name__ == '__main__':
    main()
