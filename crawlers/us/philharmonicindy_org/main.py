import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.philharmonicindy.org/'
SCHEDULE_URL = f'{SOURCE_URL}season-schedule'
SITEMAP_URL = f'{SOURCE_URL}pages-sitemap.xml'
SOURCE = 'Philharmonic Orchestra of Indianapolis'
CITY = 'Indianapolis'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

FULL_DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Z][a-z]+)\s+(\d{1,2}),\s+(20\d{2})\b'
)
SHORT_DATE_RE = re.compile(
    r'\b(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday),\s+'
    r'([A-Z][a-z]+)\s+(\d{1,2})\s*\|\s*(\d{1,2}(?::\d{2})?)\b'
)
TIME_RE = re.compile(r'\b(?:Concert\s+)?(\d{1,2}(?::\d{2})?)\s*(AM|PM)\b', re.I)
RANGE_TIME_RE = re.compile(
    r'\b(\d{1,2}(?::\d{2})?)\s*(?:AM|PM)?\s*[-–]\s*'
    r'\d{1,2}(?::\d{2})?\s*(AM|PM)\b',
    re.I,
)
MONTH_PATH_RE = re.compile(
    r'(?:january|february|march|april|may|june|july|august|september|october|november|december)'
    r'(?:-|/).*?(?:20\d{2}|\d{1,2})',
    re.I,
)

VENUES = (
    ('Pike Performing Arts Center', re.compile(r'Pike Performing\s+Arts Center', re.I)),
    ("St. Luke's United Methodist Church", re.compile(r"St\.?\s*Luke'?s\s+(?:UMC|United Methodist Church)", re.I)),
    ('MacAllister Amphitheater', re.compile(r'MacAllister Amphitheater', re.I)),
    ('Indianapolis Central Library', re.compile(r'(?:Indianapolis\s+)?Central Library', re.I)),
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def parse_date(month, day, year):
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(hour, meridiem=None):
    value = f'{hour} {meridiem or "PM"}'
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def find_venue(text):
    for venue, pattern in VENUES:
        if pattern.search(text):
            return venue
    return None


def event_title(main, text, event_date):
    for heading in main.select('h1, h2, h3, h4'):
        title = clean_text(heading)
        if (
            title
            and not find_venue(title)
            and not FULL_DATE_RE.search(title)
            and not re.search(r'tickets?|purchase|\$|adult|senior|student', title, re.I)
        ):
            return title

    before = text[:event_date.start()]
    lines = [line.strip(' ,') for line in before.splitlines() if line.strip()]
    for line in reversed(lines):
        if re.search(r'concert|music|ravel|beethoven|summer', line, re.I) and 'conductor' not in line.lower():
            return line
    after = text[event_date.end():event_date.end() + 500]
    for line in after.splitlines():
        line = line.strip(' ,')
        if (
            re.search(r'concert|music|summer', line, re.I)
            and 'conductor' not in line.lower()
            and not TIME_RE.search(line)
        ):
            return line
    return f'{SOURCE} concert'


def parse_detail_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main')
    if not main:
        return None
    text = clean_text(main)
    date_match = FULL_DATE_RE.search(text)
    venue = find_venue(text)
    if not date_match or not venue or not re.search(r'\bconcert\b|\borchestra\b|\bchamber\b', text, re.I):
        return None

    event_date = parse_date(date_match.group(1), date_match.group(2), date_match.group(3))
    if not event_date:
        return None
    time_text = text[date_match.end():date_match.end() + 100]
    range_match = RANGE_TIME_RE.search(time_text)
    time_match = range_match or TIME_RE.search(time_text)
    return {
        'title': event_title(main, text, date_match),
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_match.group(1), time_match.group(2)) if time_match else None,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': text,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def season_year(month, start_year):
    return start_year if datetime.strptime(month, '%B').month >= 7 else start_year + 1


def parse_schedule(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main')
    if not main:
        return []
    text = clean_text(main)
    season_match = re.search(r'(20\d{2})\s*[-–]\s*(?:20)?\d{2}\s+Concert Schedule', text, re.I)
    if not season_match:
        return []
    start_year = int(season_match.group(1))
    matches = list(SHORT_DATE_RE.finditer(text))
    records = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.end():end].strip()
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        title = lines[0] if lines else ''
        venue = find_venue(section)
        if not venue and '@ PPAC' in section:
            venue = 'Pike Performing Arts Center'
        event_date = parse_date(match.group(2), match.group(3), season_year(match.group(2), start_year))
        if not title or not venue or not event_date:
            continue
        records.append({
            'title': re.sub(r'\s*@\s*.*$', '', title).strip(),
            'date': event_date,
            'url': SCHEDULE_URL,
            'time_from': parse_time(match.group(4)),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': section,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def archive_urls(sitemap_xml):
    soup = BeautifulSoup(sitemap_xml, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node)
        path = urlparse(url).path.strip('/')
        if MONTH_PATH_RE.search(path) or re.search(r'gospel-concert-20\d{2}', path, re.I):
            urls.append(url)
    return urls


def scrape_concerts(session=None):
    session = session or make_session()
    schedule_response = session.get(SCHEDULE_URL, timeout=60)
    schedule_response.raise_for_status()
    records = parse_schedule(schedule_response.text)

    sitemap_response = session.get(SITEMAP_URL, timeout=60)
    sitemap_response.raise_for_status()
    for url in archive_urls(sitemap_response.text):
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            record = parse_detail_page(response.text, url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['date'], record['time_from'], record['venue'])
        existing = unique.get(key)
        if not existing or (existing['url'] == SCHEDULE_URL and record['url'] != SCHEDULE_URL):
            unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SCHEDULE_URL,
            record_count=0,
        )
    return result


class PhilharmonicIndyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='philharmonicindy_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PhilharmonicIndyOrgCrawler().run()


if __name__ == '__main__':
    main()
