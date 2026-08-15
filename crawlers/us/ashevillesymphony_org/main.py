import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ashevillesymphony.org/'
EVENT_SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
SOURCE = 'Asheville Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?\b', re.IGNORECASE)

# Venue names are displayed as free text rather than structured fields.  This
# map also prevents the orchestra's home city from being applied to its tours.
VENUES = (
    ('BLACK MOUNTAIN COLLEGE MUSEUM + ARTS CENTER', 'Black Mountain College Museum + Arts Center', 'Asheville'),
    ('FIRST BAPTIST CHURCH OF ASHEVILLE', 'First Baptist Church of Asheville', 'Asheville'),
    ('HARRAH’S CHEROKEE CENTER – ASHEVILLE', "Harrah's Cherokee Center - Asheville", 'Asheville'),
    ("MARS HILL UNIVERSITY’S MOORE AUDITORIUM", 'Moore Auditorium at Mars Hill University', 'Mars Hill'),
    ('ASHEVILLE HIGH SCHOOL AUDITORIUM', 'Asheville High School Auditorium', 'Asheville'),
    ('HIGHLAND BREWING EVENT CENTER', 'Highland Brewing Event Center', 'Asheville'),
    ('WORTHAM CENTER FOR THE PERFORMING ARTS', 'Wortham Center for the Performing Arts', 'Asheville'),
    ('PACK SQUARE PARK IN DOWNTOWN ASHEVILLE', 'Pack Square Park', 'Asheville'),
    ('PACK SQUARE PARK, DOWNTOWN ASHEVILLE', 'Pack Square Park', 'Asheville'),
    ('THOMAS WOLFE AUDITORIUM', 'Thomas Wolfe Auditorium', 'Asheville'),
    ('WHITE HORSE BLACK MOUNTAIN', 'White Horse Black Mountain', 'Black Mountain'),
    ('PISGAH BREWING', 'Pisgah Brewing', 'Black Mountain'),
    ('BREVARD MUSIC CENTER', 'Brevard Music Center', 'Brevard'),
    ('THE ORANGE PEEL', 'The Orange Peel', 'Asheville'),
    ('HIGHLAND BREWING', 'Highland Brewing', 'Asheville'),
    ('ASHEVILLE HIGH SCHOOL', 'Asheville High School', 'Asheville'),
    ('MOORE AUDITORIUM', 'Moore Auditorium at Mars Hill University', 'Mars Hill'),
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_times(value):
    times = []
    for hour, minute, meridiem in TIME_RE.findall(clean_text(value)):
        try:
            parsed = datetime.strptime(
                f'{hour}:{minute or "00"} {meridiem.upper()}M', '%I:%M %p'
            ).strftime('%H:%M')
        except ValueError:
            continue
        if parsed not in times:
            times.append(parsed)
    return times


def venue_and_city(value):
    normalized = clean_text(value).upper().replace("'", '’')
    for needle, venue, city in VENUES:
        if needle in normalized:
            return venue, city
    return None, None


def event_urls(session):
    response = session.get(EVENT_SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    return list(dict.fromkeys(
        clean_text(node.get_text())
        for node in soup.select('url > loc')
        if '/event/' in node.get_text()
    ))


def parse_event_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('.text-column-inner')
    if not content:
        return []

    title_node = content.select_one('h1, h2')
    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
    date_node = content.find(string=lambda text: text and DATE_RE.search(clean_text(text)))
    if not title or not date_node:
        return []

    date_block = date_node.find_parent(['h1', 'h2', 'h3', 'h4', 'p', 'div'])
    date_text = clean_text(date_block.get_text(' ', strip=True) if date_block else date_node)
    event_date = parse_date(date_text)
    times = parse_times(date_text)

    # Venue headings occur in the compact header before the long description.
    header_text = clean_text('\n'.join(list(content.stripped_strings)[:25]))
    venue, city = venue_and_city(header_text)
    if not event_date or not venue or not city:
        return []

    for unwanted in content.select('script, style, noscript, .button-link'):
        unwanted.decompose()
    description = clean_text(content.get_text('\n', strip=True)) or None

    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for time_from in (times or [None])]


def fetch_event(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        records = parse_event_page(url, response.text)
        if not records:
            log_message(
                'Event page lacks a complete occurrence',
                event='crawler_event_skipped',
                level='warning',
                url=url,
            )
        return records
    except requests.RequestException as error:
        log_message(
            'Event page request failed',
            event='crawler_event_request_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_event, url): url for url in urls}
        for future in as_completed(futures):
            records.extend(future.result())

    if not records:
        log_message(
            'No complete event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENT_SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class AshevilleSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ashevillesymphony_org',
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
        return scrape_concerts()


def main():
    AshevilleSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
