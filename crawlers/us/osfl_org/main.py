import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.osfl.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
STORIES_URL = urljoin(SOURCE_URL, 'storieswithmusic')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Orchestra of the Southern Finger Lakes'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_TIME_RE = re.compile(
    rf'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    rf'(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:,\s*(?P<year>20\d{{2}}))?'
    rf'\s*at\s+(?P<time>\d{{1,2}}(?::\d{{2}})?\s*[AP]M)',
    re.I,
)

VENUE_CITIES = {
    '360aurora': 'Elmira Heights',
    'addison public library': 'Addison',
    'berkshire free library': 'Berkshire',
    'big flats library': 'Big Flats',
    'cady library': 'Nichols',
    'candor fire hall': 'Candor',
    'clemens center': 'Elmira',
    'coburn free library': 'Owego',
    'corning museum of glass': 'Corning',
    'dormann library': 'Bath',
    'elizabeth b. pert library': 'Groton',
    'fred & harriett taylor memorial library': 'Hammondsport',
    'horseheads free library': 'Horseheads',
    'north presbyterian church': 'Elmira',
    'southeast steuben county library': 'Corning',
    'steele memorial library': 'Elmira',
    'the park church': 'Elmira',
    'watkins glen public library': 'Watkins Glen',
    'waverly free library': 'Waverly',
    'west elmira library': 'Elmira',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def season_year(text, month):
    explicit = re.search(r'\b(20\d{2})\s*[-–]\s*(20\d{2})\b', text)
    if explicit:
        first, second = map(int, explicit.groups())
        return first if month >= 7 else second
    single = re.search(r'\b(20\d{2})\b', text)
    return int(single.group(1)) if single else None


def parse_occurrence(match, context):
    month = datetime.strptime(match.group('month')[:3], '%b').month
    year = int(match.group('year')) if match.group('year') else season_year(context, month)
    if not year:
        return None
    try:
        event_date = datetime(year, month, int(match.group('day'))).date().isoformat()
        event_time = datetime.strptime(
            clean_text(match.group('time')).upper(),
            '%I:%M %p' if ':' in match.group('time') else '%I %p',
        ).strftime('%H:%M')
    except ValueError:
        return None
    return event_date, event_time


def venue_and_city(value):
    venue = clean_text(value).strip(' ,')
    venue = re.sub(r',?\s+(?:NY|New York)(?:\s+\d{5})?$', '', venue, flags=re.I)
    for name, city in VENUE_CITIES.items():
        if name in venue.lower():
            start = venue.lower().index(name)
            canonical = venue[start:start + len(name)].strip()
            return canonical, city
    return None, None


def make_record(title, occurrence, venue, city, url, description):
    if not title or not occurrence or not venue or not city:
        return None
    return {
        'title': title,
        'date': occurrence[0],
        'url': url,
        'time_from': occurrence[1],
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'xml' if url == SITEMAP_URL else 'html.parser')


def scrape_calendar(session):
    soup = fetch_soup(session, CALENDAR_URL)
    page_text = clean_text(soup.select_one('main').get_text(' ', strip=True))
    records = []
    for block in soup.select('main .sqs-html-content'):
        lines = [clean_text(value) for value in block.get_text('\n', strip=True).splitlines()]
        lines = [value for value in lines if value]
        match = DATE_TIME_RE.search(' '.join(lines))
        if not match or len(lines) < 3:
            continue
        occurrence = parse_occurrence(match, page_text)
        date_index = next((i for i, value in enumerate(lines) if DATE_TIME_RE.search(value)), None)
        if date_index is None or date_index + 1 >= len(lines):
            continue
        venue, city = venue_and_city(lines[date_index + 1])
        description = clean_text(' '.join(lines[date_index + 2:])) or None
        record = make_record(lines[0], occurrence, venue, city, CALENDAR_URL, description)
        if record:
            records.append(record)
    return records


def scrape_stories(session):
    soup = fetch_soup(session, STORIES_URL)
    overview = clean_text(soup.select_one('main .sqs-html-content').get_text(' ', strip=True))
    records = []
    current_title = ''
    current_description = overview
    for block in soup.select('main .sqs-html-content'):
        lines = [clean_text(value) for value in block.get_text('\n', strip=True).splitlines()]
        lines = [value for value in lines if value]
        if not lines:
            continue
        if not DATE_TIME_RE.search(' '.join(lines)):
            if len(lines) >= 2 and not lines[0].startswith(('2026 ', 'Photos ', 'Previous ')):
                current_title = f'Stories with Music: {lines[0]}'
                current_description = clean_text(f'{overview} {" ".join(lines)}')
            continue
        schedule = ' '.join(lines)
        matches = list(DATE_TIME_RE.finditer(schedule))
        for index, match in enumerate(matches):
            segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(schedule)
            venue_text = schedule[match.end():segment_end].strip(' ,')
            if venue_text:
                occurrence = parse_occurrence(match, '2026 Stories with Music')
                venue, city = venue_and_city(venue_text)
                record = make_record(
                    current_title or 'Stories with Music', occurrence, venue, city,
                    STORIES_URL, current_description,
                )
                if record:
                    records.append(record)
    return records


def scrape_archived_details(session):
    sitemap = fetch_soup(session, SITEMAP_URL)
    urls = [node.get_text(strip=True) for node in sitemap.find_all('loc')]
    records = []
    for url in urls:
        if url in {
            CALENDAR_URL,
            STORIES_URL,
            urljoin(SOURCE_URL, 'orchestra-series'),
            urljoin(SOURCE_URL, 'chamber-music-series'),
        }:
            continue
        try:
            soup = fetch_soup(session, url)
        except requests.RequestException as error:
            log_message(
                'Could not fetch OSFL archive page', event='crawler_page_fetch_failed',
                level='warning', url=url, error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        main = soup.select_one('main')
        if not main:
            continue
        text = clean_text(main.get_text(' ', strip=True))
        if not re.search(r'(?:ORCHESTRA|CHAMBER MUSIC) SERIES', text, re.I):
            continue
        match = DATE_TIME_RE.search(text)
        title_node = main.find(['h1', 'h2'])
        if not match or not title_node:
            continue
        occurrence = parse_occurrence(match, text)
        tail = text[match.end():]
        venue_text = re.split(r'Buy Tickets|Subscribe to the Season', tail, maxsplit=1)[0]
        venue, city = venue_and_city(venue_text)
        record = make_record(clean_text(title_node.get_text(' ', strip=True)), occurrence,
                             venue, city, url, text)
        if record:
            records.append(record)
    return records


class OsflOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osfl_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = scrape_calendar(session) + scrape_stories(session) + scrape_archived_details(session)
        if not records:
            log_message(
                'No OSFL performances found', event='crawler_empty_listing', level='warning',
                url=CALENDAR_URL, record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


def main():
    OsflOrgCrawler().run()


if __name__ == '__main__':
    main()
