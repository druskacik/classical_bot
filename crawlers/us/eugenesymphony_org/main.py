import html
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.eugenesymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SEASON_URL = urljoin(SOURCE_URL, '26-27subscriptions')
SOURCE = 'Eugene Symphony'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'(?P<month>[A-Za-z]+\.?)\s+(?P<day>\d{1,2})(?:,\s*(?P<year>20\d{2}))?'
    r',\s*(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)
TIMEZONE = ZoneInfo('America/Los_Angeles')


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u202f', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    value = re.sub(r'(?<=\d)(AM|PM)$', r' \1', value)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def city_from_location(location):
    address = clean_text((location or {}).get('addressLine2'))
    if address:
        for known_city in ('Eugene', 'Springfield', 'Cottage Grove', 'Creswell', 'Veneta'):
            if re.search(rf'\b{re.escape(known_city)}\b', address, re.I):
                return known_city
        city = clean_text(address.split(',')[0])
        if city and not re.fullmatch(r'(?:OR|Oregon|\d{5})', city, re.I):
            if not re.search(r'\d', city):
                return city
    title = clean_text((location or {}).get('addressTitle'))
    for city in ('Eugene', 'Springfield', 'Cottage Grove', 'Creswell', 'Veneta'):
        if re.search(rf'\b{re.escape(city)}\b', title, re.I):
            return city
    return 'Eugene' if title else ''


def item_url(item):
    value = item.get('fullUrl') or item.get('urlId') or ''
    if value and not value.startswith(('http://', 'https://')):
        value = urljoin(SOURCE_URL, value)
    return value


def api_record(item):
    title = clean_text(item.get('title'))
    url = item_url(item)
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    try:
        start = datetime.fromtimestamp(item['startDate'] / 1000, TIMEZONE)
    except (KeyError, TypeError, ValueError, OSError):
        return None

    if not all((title, url, venue, city)):
        return None
    description = clean_text(item.get('body') or item.get('excerpt')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def season_years(soup):
    title = clean_text(soup.title)
    match = re.search(r'\b(\d{2})\s*[/–-]\s*(\d{2})\b', title)
    if not match:
        return None
    first = 2000 + int(match.group(1))
    return first, 2000 + int(match.group(2))


def parse_detail_page(response, years):
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []
    text = clean_text(main)
    metadata_title = soup.select_one('meta[property="og:title"]')
    title = clean_text(metadata_title.get('content') if metadata_title else soup.title)
    title = re.sub(r'\s+[—|-]\s+Eugene Symphony\s*$', '', title).strip()

    lines = [line for line in text.splitlines() if line]
    venue = next(
        (
            line for line in lines
            if re.search(r'\b(?:hall|theater|theatre|center|amphitheater|auditorium|lobby)\b', line, re.I)
            and len(line) <= 100
        ),
        '',
    )
    if not title or not venue:
        return []

    records = []
    for match in DATE_RE.finditer(text):
        month = match.group('month').rstrip('.')
        year = match.group('year')
        if not year and years:
            month_number = datetime.strptime(month[:3], '%b').month
            year = str(years[0] if month_number >= 8 else years[1])
        if not year:
            continue
        try:
            event_date = datetime.strptime(
                f'{month[:3]} {match.group("day")} {year}', '%b %d %Y'
            ).date().isoformat()
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': response.url,
            'time_from': parse_time(match.group('time')),
            'venue': venue,
            'city': 'Florence' if re.search(r'\bFlorence\b', venue, re.I) else 'Eugene',
            'country_code': COUNTRY_CODE,
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
        # Performance dates are presented first and consecutive on these pages;
        # later dates are ticket-sale or contextual prose, not occurrences.
        remainder = text[match.end():]
        if not DATE_RE.match(remainder.lstrip('\n')):
            break
    return records


def fetch_event_collection(session):
    url = f'{EVENTS_URL}?format=json'
    seen_pages = set()
    items = []
    while url and url not in seen_pages:
        seen_pages.add(url)
        response = session.get(url, timeout=60)
        response.raise_for_status()
        payload = response.json()
        items.extend(payload.get('upcoming') or [])
        items.extend(payload.get('past') or [])
        next_url = (payload.get('pagination') or {}).get('nextPageUrl')
        url = urljoin(SOURCE_URL, next_url) + '&format=json' if next_url else None
    return items


def season_detail_urls(session):
    response = session.get(SEASON_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    years = season_years(soup)
    excluded = {'events', 'cart', 'about', 'news', 'new', 'orchestra', 'contact'}
    urls = set()
    main = soup.select_one('main')
    for link in (main.select('a[href]') if main else []):
        url = urljoin(SEASON_URL, link.get('href')).split('#')[0]
        parsed = urlparse(url)
        slug = parsed.path.strip('/')
        if parsed.netloc == urlparse(SOURCE_URL).netloc and slug and slug not in excluded:
            if '/' not in slug and slug != '26-27subscriptions':
                urls.add(url)
    return sorted(urls), years


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for item in fetch_event_collection(session):
        record = api_record(item)
        if record:
            records.append(record)

    urls, years = season_detail_urls(session)
    for url in urls:
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            records.extend(parse_detail_page(response, years))
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No candidate events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return result


class EugeneSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eugenesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    EugeneSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
