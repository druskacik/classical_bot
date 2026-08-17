import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://santafesymphony.org/'
SOURCE = 'The Santa Fe Symphony Orchestra & Chorus'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
LISTING_SLUGS = (
    '2026-2027-season',
    'cathedral-series',
    'ed-comm-upcoming-events',
)
CITY = 'Santa Fe'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

TEXT_BLOCK_RE = re.compile(r'\[vc_column_text\b[^]]*](.*?)\[/vc_column_text]', re.S)
LINK_RE = re.compile(r'\blink=[“”"](https?://[^\s“”"|]+)')
DATE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+)\s+(\d{1,2})$', re.I
)
TIME_VENUE_RE = re.compile(r'^(.+?)\s*\|\s*(.+)$')
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])M\b', re.I)


def clean_lines(value):
    text = BeautifulSoup(value or '', 'html.parser').get_text('\n')
    text = re.sub(r'\[[^]]*]', '\n', text)
    return [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip()]


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    return datetime.strptime(
        f'{hour}:{minute or "00"} {meridiem}M', '%I:%M %p'
    ).strftime('%H:%M')


def event_year(month, season_start):
    month_number = datetime.strptime(month, '%B').month
    return season_start if month_number >= 8 else season_start + 1


def page_by_slug(session, slug):
    response = session.get(API_URL, params={'slug': slug}, timeout=45)
    response.raise_for_status()
    pages = response.json()
    return pages[0] if pages else None


def description_for_url(session, url, cache):
    if urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
        return None
    slug = urlparse(url).path.strip('/').split('/')[-1]
    if not slug:
        return None
    if slug not in cache:
        page = page_by_slug(session, slug)
        cache[slug] = '\n'.join(clean_lines(page['content']['rendered'])) if page else None
    return cache[slug]


def records_from_listing(session, page, detail_cache):
    raw = page['content']['rendered']
    heading = ' '.join(clean_lines(raw)[:15])
    season = re.search(r'(20\d{2})\s*[-–]\s*20\d{2}', heading)
    season_start = int(season.group(1)) if season else datetime.now().year
    title_links = {
        re.sub(r'\s+', ' ', anchor.get_text(' ', strip=True)): anchor.get('href')
        for anchor in BeautifulSoup(raw, 'html.parser').select('a[href]')
        if anchor.get_text(' ', strip=True)
    }
    records = []

    for block_match in TEXT_BLOCK_RE.finditer(raw):
        block = block_match.group(1)
        lines = clean_lines(block)
        date_indexes = [index for index, line in enumerate(lines) if DATE_RE.match(line)]
        if not date_indexes:
            continue
        preceding_links = LINK_RE.findall(raw[:block_match.start()])
        url = preceding_links[-1].rstrip('”"') if preceding_links else page['link']
        title = ' '.join(lines[:date_indexes[0]])
        url = title_links.get(title, url)

        for date_index in date_indexes:
            if date_index == 0 or date_index + 1 >= len(lines):
                continue
            date_match = DATE_RE.match(lines[date_index])
            location_text = lines[date_index + 1]
            if '|' in location_text and location_text.rstrip().endswith('|') and date_index + 2 < len(lines):
                location_text = f'{location_text} {lines[date_index + 2]}'
            location_match = TIME_VENUE_RE.match(location_text)
            if not location_match:
                continue
            month, day = date_match.groups()
            try:
                event_date = datetime(
                    event_year(month, season_start),
                    datetime.strptime(month, '%B').month,
                    int(day),
                ).date().isoformat()
            except ValueError:
                continue

            venue = location_match.group(2).strip()
            city = CITY
            if venue == 'Lensic':
                venue = 'Lensic Performing Arts Center'
            elif venue == 'Cathedral Basilica':
                venue = 'Cathedral Basilica of St. Francis of Assisi'
            elif venue == 'Ashley Pond':
                city = 'Los Alamos'
            if not title or not venue:
                continue

            time_values = [
                parse_time(match.group(0))
                for match in TIME_RE.finditer(location_match.group(1))
            ] or [None]
            listing_description = '\n'.join(lines[date_index + 2:]) or None
            detail_description = description_for_url(session, url, detail_cache)
            description = detail_description or listing_description
            for time_from in time_values:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': description,
                })
    return records


class SantaFeSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='santafesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        detail_cache = {}
        records = []
        for slug in LISTING_SLUGS:
            try:
                page = page_by_slug(session, slug)
                if page:
                    records.extend(records_from_listing(session, page, detail_cache))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Could not scrape Santa Fe Symphony listing',
                    event='crawler_listing_failed',
                    level='warning',
                    url=f'{SOURCE_URL}{slug}/',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        if not records:
            log_message(
                'No Santa Fe Symphony concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    SantaFeSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
