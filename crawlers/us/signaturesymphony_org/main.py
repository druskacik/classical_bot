import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.signaturesymphony.org/'
SOURCE = 'Signature Symphony at TCC'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'
CITY = 'Tulsa'
DEFAULT_VENUE = 'VanTrease Performing Arts Center for Education'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH = (
    r'January|February|March|April|May|June|July|August|'
    r'September|October|November|December'
)
FULL_DATE_RE = re.compile(
    rf'\b(?P<month>{MONTH})\s+(?P<day>\d{{1,2}})'
    r'(?:\s*[-–]\s*(?P<end_day>\d{1,2}))?,\s*(?P<year>20\d{2})\b',
    re.IGNORECASE,
)
SHORT_DATE_RE = re.compile(
    r'\b(?P<day>\d{1,2})\s+'
    rf'(?P<month>{MONTH}|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(?P<hour>1[0-2]|0?[1-9])(?:[.:](?P<minute>[0-5]\d))?\s*'
    r'(?P<ampm>[ap])\.?\s*m\.?\b',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'\b(?P<start>20\d{2})(?:\s*[-–]\s*(?P<end>\d{2,4}))?\b')

VENUE_MARKERS = (
    ('marshall brewery biergarten', 'Marshall Brewing Company Biergarten'),
    ('marshall brewery production facility', 'Marshall Brewing Company Production Facility'),
    ('chalkboard wine cellar', 'The Chalkboard Wine Cellar'),
    ('tulsa botanic garden', 'Tulsa Botanic Garden'),
    ('oneok field', 'ONEOK Field'),
    ('umac arena', 'UMAC Arena'),
    ('v an trease pace main stage', DEFAULT_VENUE),
    ('vantrease pace main stage', DEFAULT_VENUE),
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour = int(match.group('hour')) % 12
    if match.group('ampm').lower() == 'p':
        hour += 12
    return f"{hour:02d}:{int(match.group('minute') or 0):02d}"


def inferred_year(category, published, month):
    if (category or '').casefold() == 'fall perspectives':
        return 2020
    match = SEASON_RE.search(category or '')
    if match:
        start = int(match.group('start'))
        if match.group('end'):
            end = int(match.group('end'))
            if end < 100:
                end = start // 100 * 100 + end
            return start if month >= 7 else end
        return start
    try:
        return datetime.fromisoformat(published).year
    except (TypeError, ValueError):
        return None


def occurrence_dates(box, category, published):
    calendar = box.select_one('.post-meta .ti-calendar') if box else None
    calendar_text = clean_text(calendar.parent if calendar else '')
    match = FULL_DATE_RE.search(calendar_text)
    if match:
        month = datetime.strptime(match.group('month')[:3], '%b').month
        days = [int(match.group('day'))]
        if match.group('end_day'):
            days.append(int(match.group('end_day')))
        values = []
        for day in days:
            try:
                values.append(date(int(match.group('year')), month, day).isoformat())
            except ValueError:
                continue
        return values, parse_time(calendar_text)

    label = box.select_one('.post-title .label') if box else None
    short_match = SHORT_DATE_RE.search(clean_text(label))
    if not short_match:
        return [], parse_time(calendar_text)
    month = datetime.strptime(short_match.group('month')[:3], '%b').month
    year = inferred_year(category, published, month)
    if year is None:
        return [], parse_time(calendar_text)
    try:
        return [date(year, month, int(short_match.group('day'))).isoformat()], parse_time(calendar_text)
    except ValueError:
        return [], parse_time(calendar_text)


def resolve_venue(title, category, calendar_text, description):
    combined = f'{title}\n{calendar_text}\n{description}'.lower()
    if 'virtual event' in combined or 'streaming preview event' in title.lower():
        return 'Online'
    for marker, venue in VENUE_MARKERS:
        if marker in combined:
            return venue
    # These fundraising/event categories frequently use off-site venues. Do
    # not incorrectly assign the orchestra's home hall when none is published.
    if 'special event' in (category or '').lower():
        return None
    return DEFAULT_VENUE


def parse_event(item, html):
    soup = BeautifulSoup(html, 'html.parser')
    box = soup.select_one('.post-snippet')
    title = ' '.join(clean_text(item.get('title', {}).get('rendered')).split())
    url = (item.get('link') or '').strip()
    if not box or not title or not url:
        return []

    category_node = box.select_one('.post-meta .ti-tag')
    category = clean_text(category_node.parent.select_one('strong') if category_node else '')
    calendar_node = box.select_one('.post-meta .ti-calendar')
    calendar_text = clean_text(calendar_node.parent if calendar_node else '')
    description = clean_text(box.select_one('.post-content')) or None
    dates, time_from = occurrence_dates(box, category, item.get('date'))
    venue = resolve_venue(title, category, calendar_text, description or '')
    if not dates or not venue:
        return []

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def api_events(session):
    records = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,date,link,title,event-category',
            },
            timeout=45,
        )
        response.raise_for_status()
        records.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return records
        page += 1


class SignatureSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='signaturesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            items = api_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Signature Symphony event API',
                event='crawler_fetch_failed', level='error', url=API_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, item['link'], timeout=45): item
                for item in items if item.get('link')
            }
            for future in as_completed(futures):
                item = futures[future]
                url = item['link']
                try:
                    response = future.result()
                    response.raise_for_status()
                    records.extend(parse_event(item, response.text))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Signature Symphony event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        log_message(
            'Signature Symphony events parsed',
            event='crawler_parse_completed', record_count=len(records),
        )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SignatureSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
