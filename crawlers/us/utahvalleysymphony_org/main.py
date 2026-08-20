import calendar
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.utahvalleysymphony.org/'
SOURCE = 'Utah Valley Symphony'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/pages')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})
MONTH_PATTERN = '|'.join(sorted((re.escape(value) for value in MONTHS), key=len, reverse=True))

VENUES = {
    'scera shell outdoor theatre': ('SCERA Shell Outdoor Theatre', 'Orem'),
    'scera outdoor shell': ('SCERA Shell Outdoor Theatre', 'Orem'),
    'orem scera outdoor shell': ('SCERA Shell Outdoor Theatre', 'Orem'),
    'covey center for the arts': ('Covey Center for the Arts', 'Provo'),
    'provo covey center for the arts': ('Covey Center for the Arts', 'Provo'),
    'the covey center': ('Covey Center for the Arts', 'Provo'),
    'covey center': ('Covey Center for the Arts', 'Provo'),
    'noorda center for the performing arts': ('Noorda Center for the Performing Arts', 'Orem'),
    'noorda concert hall': ('Noorda Center for the Performing Arts', 'Orem'),
    'noorda center': ('Noorda Center for the Performing Arts', 'Orem'),
    'the noorda': ('Noorda Center for the Performing Arts', 'Orem'),
    'the nooda center': ('Noorda Center for the Performing Arts', 'Orem'),
    'the orchard at university place': ('The Orchard at University Place', 'Orem'),
    'orem library concert hall': ('Orem Library Concert Hall', 'Orem'),
    'orem library hall': ('Orem Library Concert Hall', 'Orem'),
    'ashton auditorium': ('Ashton Auditorium at Orem Library', 'Orem'),
    'mountain view high school': ('Mountain View High School', 'Orem'),
    'mountain view h.s.': ('Mountain View High School', 'Orem'),
    'timpanogos high school': ('Timpanogos High School', 'Orem'),
    'orem high school': ('Orem High School', 'Orem'),
    'orem jr high school': ('Orem Junior High School', 'Orem'),
    'provo eldred senior center': ('Eldred Senior Center', 'Provo'),
    'rock canyon park': ('Rock Canyon Park', 'Provo'),
    'spanish fork library park': ('Spanish Fork Library Park', 'Spanish Fork'),
    'loveland performing arts center': ('Loveland Performing Arts Center', 'Provo'),
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_time(value):
    match = re.search(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)', value, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower().startswith('p') and hour != 12:
        hour += 12
    if match.group(3).lower().startswith('a') and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def season_year(slug):
    """calendar-2025 is the site's 2024-2025 season archive."""
    match = re.fullmatch(r'calendar-(\d{4})', slug)
    return int(match.group(1)) - 1 if match else None


def parse_dates(value, start_year=None):
    text = re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', clean_text(value), flags=re.I)
    explicit_year = re.search(r'\b(20\d{2})\b', text)
    default_year = int(explicit_year.group(1)) if explicit_year else start_year
    if not default_year:
        return []

    tokens = list(re.finditer(rf'\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})', text, re.I))
    if not tokens:
        return []

    values = []
    for index, token in enumerate(tokens):
        month = MONTHS[token.group(1).rstrip('.').lower()]
        day = int(token.group(2))
        following = text[token.end():tokens[index + 1].start() if index + 1 < len(tokens) else token.end() + 40]
        local_year = re.search(r'\b(20\d{2})\b', following)
        year = int(local_year.group(1)) if local_year else default_year
        if not explicit_year and start_year and month < 7:
            year = start_year + 1
        values.append((year, month, day))

        continuation = re.match(r'\s*(?:&|and|[-–])\s*(\d{1,2})(?!\s*:)', following, re.I)
        if continuation:
            last_day = int(continuation.group(1))
            if '-' in continuation.group(0) or '–' in continuation.group(0):
                values.extend((year, month, item) for item in range(day + 1, last_day + 1))
            else:
                values.append((year, month, last_day))

    parsed = []
    for year, month, day in values:
        try:
            parsed.append(date(year, month, day).isoformat())
        except ValueError:
            continue
    return list(dict.fromkeys(parsed))


def extract_location(value):
    text = clean_text(value)
    lowered = text.lower()
    for needle, location in sorted(VENUES.items(), key=lambda item: len(item[0]), reverse=True):
        if needle in lowered:
            return location
    return None


def extract_locations(value):
    text = clean_text(value).lower()
    matches = []
    for needle, location in VENUES.items():
        position = text.find(needle)
        if position >= 0:
            matches.append((position, location))
    ordered = []
    for _, location in sorted(matches):
        if location not in ordered:
            ordered.append(location)
    return ordered


def archive_blocks(content):
    soup = BeautifulSoup(content, 'html.parser')
    for block in soup.select('.wp-block-columns'):
        heading = block.find('h2')
        if not heading:
            continue
        yield block, heading


def archive_records(page):
    records = []
    page_url = page['link']
    start_year = season_year(page['slug'])
    for block, heading in archive_blocks(page['content']['rendered']):
        title = clean_text(heading)
        text = clean_text(block)
        dates = parse_dates(text, start_year)
        location = extract_location(text)
        if not title or not dates or not location:
            continue

        description_parts = [text]
        sibling = block.find_next_sibling()
        while sibling and not (
            getattr(sibling, 'name', None) == 'div'
            and 'wp-block-columns' in sibling.get('class', [])
        ):
            sibling_text = clean_text(sibling)
            if sibling_text and sibling.name == 'details':
                description_parts.append(sibling_text)
            if sibling.name == 'hr':
                break
            sibling = sibling.find_next_sibling()
        description = '\n\n'.join(dict.fromkeys(description_parts))
        time_from = parse_time(text)
        venue, city = location
        for event_date in dates:
            records.append(make_record(title, event_date, page_url, time_from, venue, city, description))
    return records


def current_records(page, pages_by_slug):
    records = []
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    heading = soup.find(['h1', 'h2'], string=re.compile(r'\b20\d{2}\b'))
    year_match = re.search(r'\b(20\d{2})\b', clean_text(heading)) if heading else None
    start_year = int(year_match.group(1)) if year_match else datetime.now().year

    for card in soup.select('.ecs-each-concert'):
        card_text = clean_text(card)
        links = [urljoin(SOURCE_URL, item.get('href')) for item in card.select('a[href]')]
        detail_url = next((url for url in reversed(links) if urlparse(url).netloc == urlparse(SOURCE_URL).netloc), '')
        slug = urlparse(detail_url).path.strip('/').split('/')[-1]
        detail_page = pages_by_slug.get(slug)
        if not detail_page:
            continue
        detail_text = clean_text(BeautifulSoup(detail_page['content']['rendered'], 'html.parser'))
        detail_soup = BeautifulSoup(detail_page['content']['rendered'], 'html.parser')
        title_node = detail_soup.find(['h1', 'h2'])
        title = clean_text(title_node)
        dates = parse_dates(detail_text, start_year)
        locations = extract_locations(f'{card_text}\n{detail_text}')
        if not title or not dates or not locations:
            continue
        for index, event_date in enumerate(dates):
            venue, city = locations[min(index, len(locations) - 1)]
            records.append(
                make_record(
                    title,
                    event_date,
                    detail_page['link'],
                    parse_time(card_text) or parse_time(detail_text),
                    venue,
                    city,
                    detail_text,
                )
            )
    return records


def make_record(title, event_date, url, time_from, venue, city, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_pages(session):
    response = session.get(API_URL, params={'per_page': 100, 'page': 1}, timeout=60)
    response.raise_for_status()
    total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
    pages = response.json()
    for page_number in range(2, total_pages + 1):
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page_number},
            timeout=60,
        )
        response.raise_for_status()
        pages.extend(response.json())
    return pages


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    pages = fetch_pages(session)
    pages_by_slug = {page['slug']: page for page in pages}

    records = []
    current_page = pages_by_slug.get('current-season')
    if current_page:
        records.extend(current_records(current_page, pages_by_slug))
    for page in pages:
        if re.fullmatch(r'calendar-\d{4}', page['slug']):
            records.extend(archive_records(page))

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['venue']))
    if not result:
        log_message(
            'No concerts found in WordPress pages',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return result


class UtahValleySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='utahvalleysymphony_org',
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
    UtahValleySymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
