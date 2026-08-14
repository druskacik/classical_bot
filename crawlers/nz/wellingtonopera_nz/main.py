import html
import json
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wellingtonopera.nz/'
SOURCE = 'Wellington Opera'
CITY = 'Wellington'
COUNTRY_CODE = 'NZ'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-NZ,en;q=0.9',
}

PERFORMANCE_RE = re.compile(
    r'(?P<weekday>MON|TUE|WED|THU|FRI|SAT|SUN)\s+'
    r'(?P<day>\d{1,2})(?:ST|ND|RD|TH)?\s+'
    r'(?P<month>[A-Z]+),?\s+'
    r'(?P<time>\d{1,2}(?:[.:]\d{2})?\s*[AP]M)',
    re.IGNORECASE,
)

MONTHS = {
    'JAN': 1,
    'JANUARY': 1,
    'FEB': 2,
    'FEBRUARY': 2,
    'MAR': 3,
    'MARCH': 3,
    'APR': 4,
    'APRIL': 4,
    'MAY': 5,
    'JUN': 6,
    'JUNE': 6,
    'JUL': 7,
    'JULY': 7,
    'AUG': 8,
    'AUGUST': 8,
    'SEP': 9,
    'SEPT': 9,
    'SEPTEMBER': 9,
    'OCT': 10,
    'OCTOBER': 10,
    'NOV': 11,
    'NOVEMBER': 11,
    'DEC': 12,
    'DECEMBER': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    root = BeautifulSoup(response.text, 'xml')
    child_urls = [node.get_text(strip=True) for node in root.find_all('loc')]
    selected = []
    for child_url in child_urls:
        if not (
            child_url.endswith('/event-pages-sitemap.xml')
            or 'dynamic-production_' in child_url
        ):
            continue
        response = session.get(child_url, timeout=60)
        response.raise_for_status()
        child = BeautifulSoup(response.text, 'xml')
        selected.extend(node.get_text(strip=True) for node in child.find_all('loc'))
    return list(dict.fromkeys(selected))


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2})(?:[.:](\d{2}))?\s*([AP]M)', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'PM':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def json_ld_event(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def event_record(url, soup):
    event = json_ld_event(soup)
    if not event:
        return None
    title = clean_text(event.get('name'))
    start = str(event.get('startDate') or '')
    location = event.get('location') or {}
    venue = clean_text(location.get('name'))
    address = clean_text(location.get('address'))
    if venue.lower().endswith(', wellington'):
        venue = venue.rsplit(',', 1)[0].strip()
    if not title or not venue or 'wellington' not in address.lower():
        return None
    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except ValueError:
        return None
    time_match = re.search(r'T(\d{2}:\d{2})', start)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(1) if time_match else None,
        'venue': venue,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def production_records(url, soup, schedule_text=''):
    main = soup.select_one('main')
    if not main:
        return []
    text = clean_text(main.get_text('\n', strip=True))
    year_matches = re.findall(r'\b(20\d{2})\b', text)
    title_meta = soup.select_one('meta[property="og:title"]')
    title = clean_text(title_meta.get('content')) if title_meta else ''
    title = re.sub(r'\s*\|\s*Wellington Opera\s*$', '', title, flags=re.I)
    venue_match = re.search(
        r'\b((?:St James Theatre|Michael Fowler Centre|Opera House))'
        r'(?:,\s*Wellington)?\b',
        text,
        re.I,
    )
    if not title or not year_matches or not venue_match:
        return []
    year = int(year_matches[0])
    venue = venue_match.group(1)
    records = []
    seen = set()
    for match in PERFORMANCE_RE.finditer(f'{text}\n{schedule_text}'):
        month = MONTHS.get(match.group('month').upper())
        if not month:
            continue
        try:
            event_date = date(year, month, int(match.group('day'))).isoformat()
        except ValueError:
            continue
        time_from = parse_time(match.group('time'))
        key = (event_date, time_from)
        if key in seen:
            continue
        seen.add(key)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': COUNTRY_CODE,
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    home = get_soup(session, SOURCE_URL)
    home_text = clean_text(home.get_text('\n', strip=True))
    featured_productions = {
        urljoin(SOURCE_URL, link.get('href'))
        for link in home.select('a[href*="/production/"]')
    }
    for url in sitemap_urls(session):
        try:
            soup = get_soup(session, url)
            if '/event-details/' in url:
                record = event_record(url, soup)
                if record:
                    records.append(record)
            elif '/production/' in url:
                schedule_text = home_text if url in featured_productions else ''
                records.extend(production_records(url, soup, schedule_text))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert page',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    # A Wix event may be a ticket/package page for an occurrence already
    # published on the production page. Prefer the richer production record.
    records.sort(key=lambda item: '/production/' not in item['url'])
    unique = {}
    for record in records:
        key = (record['date'], record['time_from'], record['venue'], record['city'])
        unique.setdefault(key, record)
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class WellingtonoperaNzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wellingtonopera_nz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='potential',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    WellingtonoperaNzCrawler().run()


if __name__ == '__main__':
    main()
