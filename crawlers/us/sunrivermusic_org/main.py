import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sunrivermusic.org/'
SOURCE = 'Sunriver Music Festival'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

EVENT_ROOT_SLUGS = {
    '2023-festival',
    '2024-festival',
    '2025-festival',
    '2026-summer-festival',
    'music-education-events',
    'year-round-events',
    'valentinesdinner',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)'
    r'(?:day)?[,]?\s+(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', re.IGNORECASE)

VENUES = {
    'sunriver resort great hall': ('Sunriver Resort Great Hall', 'Sunriver'),
    'tower theatre': ('Tower Theatre', 'Bend'),
    'sunriver sharc': ('Sunriver SHARC', 'Sunriver'),
    'sharc': ('Sunriver SHARC', 'Sunriver'),
    'sunriver christian fellowship': ('Sunriver Christian Fellowship', 'Sunriver'),
    'sunriver nature center': ('Sunriver Nature Center', 'Sunriver'),
    'sunriver brewing company': ('Sunriver Brewing Company Taproom', 'Sunriver'),
    'willamette valley vineyards': ('Willamette Valley Vineyards Tasting Room & Restaurant', 'Bend'),
    'holy trinity catholic church': ('Holy Trinity Catholic Church', 'Sunriver'),
}


def clean_text(value):
    text = html.unescape(str(value or '')).replace('\\’', '’').replace('\xa0', ' ')
    text = re.sub(r'\[(?:/?vc_[^\]]+|/?rev_slider[^\]]*)\]', '\n', text)
    text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def page_year(page):
    value = f"{page.get('slug', '')} {page.get('title', {}).get('rendered', '')}"
    match = re.search(r'\b(20\d{2})\b', value)
    return int(match.group(1)) if match else None


def parse_date(match, fallback_year):
    year = int(match.group(3)) if match.group(3) else fallback_year
    if not year:
        return None
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {year}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == 'p' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def find_location(value):
    lowered = value.lower()
    for needle, location in VENUES.items():
        if needle in lowered:
            return location
    return None


def title_before(text, position, page_title):
    before = text[:position]
    lines = [line.strip(' –—:') for line in before.splitlines() if line.strip()]
    for candidate in reversed(lines[-5:]):
        if len(candidate) < 100 and not re.search(
            r'\b(?:20\d{2}|tickets?|festival presents|sold out|with|before|at the|annual report)\b',
            candidate,
            re.I,
        ):
            return candidate
    return page_title


def records_from_page(page):
    page_title = clean_text(page['title']['rendered'])
    text = clean_text(page['content']['rendered'])
    matches = list(DATE_RE.finditer(text))
    records = []
    for index, match in enumerate(matches):
        event_date = parse_date(match, page_year(page))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[match.start():end]
        location = find_location(chunk[:600])
        if not event_date or not location:
            continue
        venue, city = location
        title = page_title if len(matches) == 1 else title_before(text, match.start(), page_title)
        if not title:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': page['link'],
            'time_from': parse_time(chunk[:250]),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': chunk,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(API_URL, params={'per_page': 100}, timeout=45)
    response.raise_for_status()
    pages = response.json()

    roots = {page['id'] for page in pages if page['slug'].lower() in EVENT_ROOT_SLUGS}
    selected = sorted([
        page for page in pages
        if page['id'] in roots or page.get('parent') in roots
    ], key=lambda page: page.get('parent') not in roots)
    records = []
    for page in selected:
        records.extend(records_from_page(page))

    unique = {}
    for record in records:
        key = (record['date'], record['time_from'], record['venue'])
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['url']))
    timed_locations = {
        (item['date'], item['venue']) for item in result if item['time_from'] is not None
    }
    result = [
        item for item in result
        if item['time_from'] is not None or (item['date'], item['venue']) not in timed_locations
    ]
    if not result:
        log_message(
            'No event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return result


class SunriverMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sunrivermusic_org',
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
    SunriverMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
