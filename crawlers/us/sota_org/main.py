import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sota.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Symphony of the Americas'
CITY = 'Fort Lauderdale'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7,
    'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'sept': 9,
    'september': 9, 'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}
MONTH_PATTERN = '|'.join(sorted(MONTHS, key=len, reverse=True))
DATE_RE = re.compile(
    rf'\b(?P<month>{MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}})'
    r'(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>20\d{2}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.IGNORECASE)
SEASON_RE = re.compile(r'\b(20\d{2})\s*[-–]\s*(?:20)?(\d{2,4})\b')

KNOWN_VENUES = (
    'Broward Center for the Performing Arts – Amaturo Theater',
    'Broward Center for the Performing Arts - Amaturo Theater',
    'Au-Rene Theater (Broward Center)',
    'United Church of Christ, Fort Lauderdale',
    'The stunning Rooftop Pool Deck at Flow Fort Lauderdale',
    'Horvitz Auditorium – NSU Art Museum',
    'Horvitz Auditorium - NSU Art Museum',
    'The Broward Center for the Performing Arts',
    'Broward Center for the Performing Arts',
    'Amaturo Theater',
)


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page_number,
                '_fields': 'id,link,slug,title,content,parent',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page_number))
        if page_number >= total_pages:
            break
        page_number += 1
    return pages


def season_years(pages):
    """Map linked detail pages to the season year that supplies omitted years."""
    years = {}
    season_urls = {}
    for page in pages:
        title = clean_text(BeautifulSoup(page['title']['rendered'], 'html.parser').get_text(' '))
        match = SEASON_RE.search(title)
        if not match:
            continue
        start = int(match.group(1))
        end_text = match.group(2)
        end = int(end_text) if len(end_text) == 4 else (start // 100) * 100 + int(end_text)
        season = (start, end)
        season_urls[page['link'].rstrip('/') + '/'] = season
        soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
        for link in soup.select('a[href]'):
            slug = link.get('href', '').rstrip('/').rsplit('/', 1)[-1]
            if slug:
                years[slug] = season

    # Some special-event pages link back to their season even when the season
    # overview does not link to them.
    for page in pages:
        soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
        for link in soup.select('a[href]'):
            season = season_urls.get(link.get('href', '').rstrip('/') + '/')
            if season:
                years[page['slug']] = season
                break
    return years


def event_venue(text):
    for venue in KNOWN_VENUES:
        if venue.lower() in text.lower():
            return venue.replace(' - ', ' – ')
    return ''


def title_text(page):
    title = clean_text(BeautifulSoup(page['title']['rendered'], 'html.parser').get_text(' '))
    title = re.sub(
        rf'^(?:{MONTH_PATTERN})\s+\d{{1,2}},?\s+20\d{{2}}\s*'
        rf'(?:{MONTH_PATTERN})\s+\d{{1,2}},?\s+20\d{{2}}\s*[—–-]?\s*',
        '',
        title,
        flags=re.IGNORECASE,
    )
    return title.strip(' —–-')


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_occurrences(text, link, linked_seasons, venue):
    marker = text.find('Dates, Times, and Location')
    start = marker if marker >= 0 else 0
    venue_position = text.lower().find(venue.lower(), start)
    end = venue_position + len(venue) if venue_position >= 0 else start + 650
    date_area = text[start:end]
    matches = list(DATE_RE.finditer(date_area))
    explicit_years = [int(item.group('year')) for item in matches if item.group('year')]
    season = linked_seasons.get(link.rstrip('/').rsplit('/', 1)[-1])

    occurrences = []
    for index, match in enumerate(matches):
        month = MONTHS[match.group('month').lower().rstrip('.')]
        if match.group('year'):
            year = int(match.group('year'))
        elif explicit_years:
            year = explicit_years[-1]
        elif season:
            year = season[0] if month >= 7 else season[1]
        else:
            continue

        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(date_area)
        time_from = parse_time(date_area[match.end():next_start])
        try:
            event_date = datetime(year, month, int(match.group('day'))).date().isoformat()
        except ValueError:
            continue
        occurrence = (event_date, time_from)
        if occurrence not in occurrences:
            occurrences.append(occurrence)
    return occurrences


def page_records(page, linked_seasons):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    text = clean_text(soup.get_text('\n', strip=True))
    venue = event_venue(text)
    if 'About the Concert' not in text or not venue:
        return []

    title = title_text(page)
    link = page.get('link', '')
    occurrences = parse_occurrences(text, link, linked_seasons, venue)
    if not title or not link.startswith(('http://', 'https://')) or not occurrences:
        return []

    return [{
        'title': title,
        'date': event_date,
        'url': link,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date, time_from in occurrences]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    pages = fetch_pages(session)
    linked_seasons = season_years(pages)
    records = []
    for page in pages:
        records.extend(page_records(page, linked_seasons))

    if not records:
        log_message(
            'No concrete concert pages found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SotaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sota_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SotaOrgCrawler().run()


if __name__ == '__main__':
    main()
