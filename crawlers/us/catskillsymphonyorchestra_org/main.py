import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.catskillsymphonyorchestra.org/'
SOURCE = 'Catskill Symphony Orchestra'
ARCHIVE_URL = urljoin(SOURCE_URL, 'concerts')
TIMEZONE = ZoneInfo('America/New_York')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/json',
}
DATE_PATTERN = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b'
    r'|\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b'
)
CONCERT_TIME_PATTERN = re.compile(
    r'\bCONCERT\s+AT\s+(\d{1,2}(?::\d{2})?)(?:\s*([AP]M))?\b', re.I
)


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def parse_time(value):
    match = re.fullmatch(r'\s*(\d{1,2})(?::(\d{2}))?\s*([AP])M\s*', value or '', re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'P' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def record_from_archive_item(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    address = clean_text(location.get('addressLine2'))
    city = address.split(',', 1)[0].strip()
    timestamp = item.get('startDate')
    if not all((title, path, venue, city, timestamp)):
        return None
    try:
        start = datetime.fromtimestamp(int(timestamp) / 1000, tz=TIMEZONE)
    except (TypeError, ValueError, OSError):
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def season_years(soup):
    text = soup.get_text(' ', strip=True)
    match = re.search(r'\b(20\d{2})\s*[-–]\s*(20\d{2})\s+Season\b', text, re.I)
    return (int(match.group(1)), int(match.group(2))) if match else None


def record_from_season_item(item, page_url, years):
    heading = item.select_one('.list-item-content__title')
    description = item.select_one('.list-item-content__description')
    paragraphs = description.find_all('p') if description else []
    title = clean_text(heading)
    if len(paragraphs) < 2 or not title:
        return None
    date_match = DATE_PATTERN.search(clean_text(paragraphs[0]))
    if not date_match:
        return None
    try:
        date = datetime.strptime(next(value for value in date_match.groups() if value), '%B %d, %Y').date()
    except (ValueError, StopIteration):
        return None
    # Correct an obvious start-year typo for a spring event on a page explicitly
    # labelled as a two-year season (currently "March 13, 2026" in 2026-2027).
    if years and date.year == years[0] and date.month < 7:
        date = date.replace(year=years[1])

    venue_text = clean_text(paragraphs[1])
    venue = re.sub(r'\s+in\s+Oneonta\s*$', '', venue_text, flags=re.I).strip()
    if not venue:
        return None
    link = item.select_one('a[href]')
    href = link.get('href') if link else ''
    event_url = urljoin(SOURCE_URL, href) if href.startswith('/') else page_url
    body = '\n'.join(clean_text(p) for p in paragraphs[2:] if clean_text(p)) or None
    time_from = None

    # A first-party detail page can add the advertised concert time and full
    # programme. External ticketing pages are deliberately not used as sources.
    if event_url.startswith(SOURCE_URL) and event_url != page_url:
        detail = requests.get(event_url, headers=HEADERS, timeout=45)
        detail.raise_for_status()
        detail_soup = BeautifulSoup(detail.text, 'html.parser')
        main = detail_soup.select_one('main article') or detail_soup.select_one('main')
        detail_text = clean_text(main)
        time_match = CONCERT_TIME_PATTERN.search(detail_text)
        if time_match:
            # This page writes "doors at 6:30, concert at 7:30" without a
            # meridiem. The evening ordering supplies strong PM evidence.
            meridiem = time_match.group(2) or 'PM'
            time_from = parse_time(f'{time_match.group(1)} {meridiem}')
        body = detail_text or body

    return {
        'title': title,
        'date': date.isoformat(),
        'url': event_url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Oneonta',
        'country_code': 'US',
        'description': body,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def discover_season_urls(session):
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = {
        urljoin(SOURCE_URL, link.get('href'))
        for link in soup.select('a[href]')
        if re.fullmatch(r'/\d{4}season/?', link.get('href', ''))
    }
    return sorted(urls)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    response = session.get(f'{ARCHIVE_URL}?format=json', timeout=45)
    response.raise_for_status()
    payload = response.json()
    for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
        record = record_from_archive_item(item)
        if record:
            records.append(record)

    for page_url in discover_season_urls(session):
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            years = season_years(soup)
            for item in soup.select('.list-item-content'):
                record = record_from_season_item(item, page_url, years)
                if record:
                    records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Catskill Symphony season page',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda row: (row['date'], row['time_from'] or '', row['title']))
    if not result:
        log_message(
            'No valid Catskill Symphony concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class CatskillSymphonyOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='catskillsymphonyorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CatskillSymphonyOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
