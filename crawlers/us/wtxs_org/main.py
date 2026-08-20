import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wtxs.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts/')
SOURCE = 'West Texas Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'([A-Z][a-z]+)\s+(\d{1,2}),\s+(20\d{2})'
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?)\s*([AP])\.?M\.?\b', re.I)
SEASON_RE = re.compile(r'/concerts/(\d{2})-(\d{2})-season\.html$')


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
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    raw = f'{match.group(1)} {match.group(2).upper()}M'
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(raw, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def city_for_venue(venue):
    lowered = clean_text(venue).lower()
    if 'odessa' in lowered:
        return 'Odessa'
    if 'midland' in lowered or 'wagner noël' in lowered or 'wagner noel' in lowered:
        return 'Midland'
    if 'rea-greathouse' in lowered or 'rea greathouse' in lowered:
        return 'Midland'
    return None


def get_soup(session, url, log_errors=True):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')
    except requests.RequestException as error:
        if log_errors:
            log_message(
                'Unable to fetch concert page',
                event='crawler_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
        return None


def detail_record(soup, url):
    main = soup.select_one('main')
    if not main:
        return None
    text = clean_text(main.get_text('\n', strip=True))
    date_match = DATE_RE.search(text)
    if not date_match:
        return None

    event_date = parse_date(date_match.group(0))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    date_index = next((i for i, line in enumerate(lines) if DATE_RE.search(line)), None)
    if not event_date or date_index is None:
        return None

    title = clean_text(soup.select_one('h1').get_text(' ', strip=True) if soup.select_one('h1') else '')
    if not title:
        title = next((line for line in reversed(lines[:date_index]) if len(line) > 2), '')

    date_line = lines[date_index]
    following = lines[date_index + 1:date_index + 4]
    time_from = parse_time(date_line) or next((parse_time(line) for line in following if parse_time(line)), None)
    venue = ''
    candidates = [date_line, *following]
    for line in candidates:
        if '|' in line:
            parts = [clean_text(part) for part in line.split('|')]
            venue = next((part for part in parts if part and not TIME_RE.search(part)), '')
        elif any(term in line.lower() for term in ('center', 'hall', 'church')):
            venue = clean_text(TIME_RE.sub('', line).strip(' |,-'))
        if venue:
            break

    city = city_for_venue(venue)
    if not title or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': text,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def chamber_records(soup, season_url):
    main = soup.select_one('main')
    if not main:
        return []
    lines = [clean_text(line) for line in main.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    try:
        start = next(i for i, line in enumerate(lines) if line.upper() == 'CHAMBER CONCERTS')
    except StopIteration:
        return []

    records = []
    for index in range(start + 1, len(lines)):
        event_date = parse_date(lines[index])
        if not event_date:
            continue
        title = lines[index - 1]
        if title.startswith(('"', '“')) and index >= 2:
            title = f'{lines[index - 2]} {title}'
        location_text = ' | '.join(lines[index:index + 2])
        parts = [clean_text(part) for part in location_text.split('|')]
        venue = next(
            (part for part in parts if part and not DATE_RE.search(part) and not TIME_RE.fullmatch(part)),
            '',
        )
        city = city_for_venue(venue)
        if not title or not venue or not city or 'tba' in venue.lower():
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': season_url,
            'time_from': parse_time(lines[index]),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': f'{title}\n{location_text}',
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    listing = get_soup(session, CONCERTS_URL)
    if not listing:
        return []

    season_urls = {
        urljoin(CONCERTS_URL, link.get('href'))
        for link in listing.select('a[href]')
        if SEASON_RE.search(urlparse(urljoin(CONCERTS_URL, link.get('href'))).path)
    }
    current_year = datetime.now().year
    for year in range(current_year - 5, current_year + 2):
        season_urls.add(urljoin(CONCERTS_URL, f'{year % 100:02d}-{(year + 1) % 100:02d}-season.html'))

    records = []
    detail_urls = set()
    for season_url in sorted(season_urls):
        soup = get_soup(session, season_url, log_errors=False)
        if not soup:
            continue
        if not parse_date(soup.get_text(' ', strip=True)):
            continue
        records.extend(chamber_records(soup, season_url))
        for link in soup.select('a[href]'):
            url = urljoin(season_url, link.get('href'))
            path = urlparse(url).path
            if path.startswith('/concerts/') and path.endswith('.html') and not SEASON_RE.search(path):
                detail_urls.add(url)

    for url in sorted(detail_urls):
        soup = get_soup(session, url)
        record = detail_record(soup, url) if soup else None
        if record:
            records.append(record)

    unique = {(r['title'], r['date'], r['time_from'], r['venue']): r for r in records}
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['url']))
    if not result:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return result


class WtxsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wtxs_org',
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
    WtxsOrgCrawler().run()


if __name__ == '__main__':
    main()
