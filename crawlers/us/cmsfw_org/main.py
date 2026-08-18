import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cmsfw.org/'
SOURCE = 'Chamber Music Society of Fort Worth'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
CITY = 'Fort Worth'
DEFAULT_VENUE = 'Modern Art Museum of Fort Worth'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(rf'\b({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,\s+(20\d{{2}})\b', re.I)
TIME_PATTERNS = (
    re.compile(r'\bConcert\s+(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)', re.I),
    re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\s+Concert\b', re.I),
    re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\s+at\s+the\s+Modern Art Museum', re.I),
)

VENUES = (
    'Fort Worth Museum of Science & History Omni Theater',
    'Fort Worth Museum of Science and History Omni Theater',
    'Modern Art Museum of Fort Worth',
    'Amon Carter Museum of American Art',
    'Kimbell Art Museum',
)

NON_EVENT_SLUGS = {
    'chamber-music-society-of-fort-worth-home',
    'tickets',
    'fcs',
    'gift',
    'privacy-policy',
    'donate',
    'contact',
    'cmsfw-performers-and-repertory',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def extract_dates(text):
    dates = []
    for month, day, year in DATE_RE.findall(text):
        try:
            value = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
        except ValueError:
            continue
        if value not in dates:
            dates.append(value)
    return dates


def parse_time(text):
    for pattern in TIME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = re.sub(r'\.', '', match.group(1)).upper()
        for time_format in ('%I:%M %p', '%I %p'):
            try:
                return datetime.strptime(value, time_format).strftime('%H:%M')
            except ValueError:
                pass
    return None


def extract_venue(text):
    normalized = re.sub(r'\s+', ' ', text.replace('&amp;', '&'))
    for venue in VENUES:
        if venue.lower() in normalized.lower():
            if venue == 'Fort Worth Museum of Science and History Omni Theater':
                return 'Fort Worth Museum of Science & History Omni Theater'
            return venue
    return DEFAULT_VENUE


def event_from_page(page):
    slug = page.get('slug', '')
    title = clean_text(page.get('title', {}).get('rendered'))
    description = clean_text(page.get('content', {}).get('rendered'))
    dates = extract_dates(description)

    # Concrete CMSFW event pages consistently have one occurrence date and a
    # ticket action. This excludes season, ticket, repertory, and education
    # overview pages, which contain multiple dates or no specific occurrence.
    if (
        slug in NON_EVENT_SLUGS
        or len(dates) != 1
        or 'purchase tickets' not in description.lower()
        or 'concert' not in description.lower()
    ):
        return None

    title = DATE_RE.sub('', title, count=1).strip(' -–—')
    url = page.get('link', '')
    if not title or not url.startswith(('http://', 'https://')):
        return None

    return {
        'title': title,
        'date': dates[0],
        'url': url,
        'time_from': parse_time(description),
        'venue': extract_venue(description),
        'city': CITY,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page_number = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page_number,
                '_fields': 'slug,link,title,content',
            },
            timeout=45,
        )
        response.raise_for_status()
        pages = response.json()
        for page in pages:
            record = event_from_page(page)
            if record:
                records.append(record)

        total_pages = int(response.headers.get('X-WP-TotalPages', page_number))
        if page_number >= total_pages:
            break
        page_number += 1

    if not records:
        log_message(
            'No concert pages found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class CmsfwOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cmsfw_org',
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
    CmsfwOrgCrawler().run()


if __name__ == '__main__':
    main()
