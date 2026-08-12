import html
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wgconcertclub.org.uk/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Welwyn Garden Concert Club'
VENUE = 'St Francis of Assisi Church'
CITY = 'Welwyn Garden City'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
TEXT_DATE_RE = re.compile(
    rf'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*'
    rf'(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})[,]?\s+(20\d{{2}})',
    re.IGNORECASE,
)
SHORT_DATE_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{2}|20\d{2})\b')
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'(\d)\s*\n\s*(st|nd|rd|th)\s*\n\s*', r'\1\2 ', text, flags=re.IGNORECASE)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = TEXT_DATE_RE.search(value)
    if match:
        try:
            return datetime.strptime(' '.join(match.groups()), '%d %B %Y').date().isoformat()
        except ValueError:
            return None
    match = SHORT_DATE_RE.search(value)
    if not match:
        return None
    day, month, year = match.groups()
    year = f'20{year}' if len(year) == 2 and int(year) < 70 else (
        f'19{year}' if len(year) == 2 else year
    )
    try:
        return datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour)
    minute = int(minute or 0)
    if hour not in range(1, 13) or minute > 59:
        return None
    if meridiem.lower() == 'pm' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def record(title, date, url, description, time_from=None):
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'GB',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def page_soup(page):
    soup = BeautifulSoup(page.get('content', {}).get('rendered', ''), 'html.parser')
    for node in soup.select('script, style, img'):
        node.decompose()
    for node in soup.select('sup'):
        node.unwrap()
    return soup


def parse_detail_page(page):
    soup = page_soup(page)
    text = clean_text(soup)
    date = parse_date(text)
    title = clean_text(BeautifulSoup(page['title']['rendered'], 'html.parser'))
    if not title or not date:
        return []
    return [record(title, date, page['link'], text, parse_time(text))]


def parse_season_page(page):
    """Parse old archive pages whose individual concerts have no detail page."""
    lines = [line for line in clean_text(page_soup(page)).splitlines() if line]
    starts = [(index, parse_date(line)) for index, line in enumerate(lines)]
    starts = [(index, date) for index, date in starts if date]
    records = []
    for position, (index, date) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        section = lines[index:end]
        if len(section) < 2:
            continue
        title = section[1].strip(' -–|')
        if not title or parse_date(title):
            continue
        description = '\n'.join(section[1:])
        records.append(record(title, date, page['link'], description, parse_time(section[0])))
    return records


def api_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page_number, 'orderby': 'id', 'order': 'desc'},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page_number))
        if page_number >= total_pages:
            return pages
        page_number += 1


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        pages = api_pages(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch Welwyn Garden Concert Club pages',
            event='crawler_source_failed',
            level='error',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    detail_pages = []
    season_pages = []
    detail_seasons = set()
    for page in pages:
        path = urlparse(page.get('link', '')).path.strip('/').split('/')
        if len(path) == 2 and path[0] == 'current-season':
            detail_pages.append(page)
        elif len(path) == 3 and path[0] == 'previous-concerts' and path[1].startswith('season-'):
            detail_pages.append(page)
            detail_seasons.add(path[1])
        elif len(path) == 2 and path[0] == 'previous-concerts' and path[1].startswith('season-'):
            season_pages.append(page)

    records = []
    for page in detail_pages:
        records.extend(parse_detail_page(page))
    for page in season_pages:
        season = urlparse(page['link']).path.strip('/').split('/')[1]
        if season not in detail_seasons:
            records.extend(parse_season_page(page))

    unique = {}
    for item in records:
        unique[(item['title'], item['date'], item['venue'])] = item
    return sorted(unique.values(), key=lambda item: (item['date'], item['title']))


class WgConcertClubOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wgconcertclub_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    WgConcertClubOrgUkCrawler().run()


if __name__ == '__main__':
    main()
