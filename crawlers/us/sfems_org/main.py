import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sfems.org/'
SOURCE = 'San Francisco Early Music Society'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
CURRENT_SEASON_URL = urljoin(SOURCE_URL, 'concerts')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

PERFORMANCE_RE = re.compile(
    r'^(?:(?:PART\s+[IVX]+|[^:]{1,30}):\s*)?'
    r'(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),?\s+'
    r'([A-Z]+\.?)\s+(\d{1,2}),\s*'
    r'(\d{1,2}(?::\d{2})?)\s*(AM|PM)\b',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'\b(20\d{2})\s*[/–-]\s*(?:20)?(\d{2})\b')

VENUE_CITIES = {
    'first church berkeley ucc (first congregational)': 'Berkeley',
    'first church berkeley ucc': 'Berkeley',
    'first congregational church of berkeley': 'Berkeley',
    'first presbyterian church of berkeley': 'Berkeley',
    'first presbyterian church': 'Palo Alto',
    'st. gregory of nyssa episcopal church': 'San Francisco',
    'st. mark’s lutheran church': 'San Francisco',
    "st. mark's lutheran church": 'San Francisco',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    parser = 'xml' if url == SITEMAP_URL else 'html.parser'
    return BeautifulSoup(response.text, parser)


def season_years(soup, url):
    text = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    text += ' ' + clean_text((soup.select_one('main') or soup).get_text(' ', strip=True))[:1000]
    match = SEASON_RE.search(text)
    if not match:
        match = re.search(r'concerts-(\d{2})-(\d{2})', url)
        if match:
            return 2000 + int(match.group(1)), 2000 + int(match.group(2))
        return None
    start = int(match.group(1))
    return start, (start // 100) * 100 + int(match.group(2))


def discover_seasons(session):
    urls = {CURRENT_SEASON_URL}
    soup = get_soup(session, SITEMAP_URL)
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        if re.fullmatch(r'https://www\.sfems\.org/concerts-\d{2}-\d{2}/?', url):
            urls.add(url.rstrip('/'))
    return sorted(urls)


def discover_event_urls(soup, season_url):
    main = soup.select_one('main') or soup.select_one('#page')
    if not main:
        return []
    ignored = {'donate', 'memberships', 'pay-what-you-can', 'concerts'}
    urls = []
    for anchor in main.select('a[href]'):
        url = urljoin(season_url, anchor.get('href'))
        parsed = urlparse(url)
        slug = parsed.path.strip('/')
        if parsed.netloc != 'www.sfems.org' or not slug or slug in ignored:
            continue
        url = f'https://www.sfems.org/{slug}'
        if url not in urls:
            urls.append(url)
    return urls


def parse_time(value, meridiem):
    value = value if ':' in value else f'{value}:00'
    return datetime.strptime(f'{value} {meridiem.upper()}', '%I:%M %p').strftime('%H:%M')


def infer_city(venue, location):
    combined = clean_text(f'{venue} {location}')
    for city in ('Palo Alto', 'Berkeley', 'San Francisco'):
        if re.search(rf'\b{re.escape(city)}\b', combined, re.IGNORECASE):
            return city
    return VENUE_CITIES.get(venue.casefold())


def page_title(soup):
    title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    title = re.sub(r'\s+—\s+The San Francisco Early Music Society.*$', '', title)
    return re.sub(r'\s+\|\s+(?:[A-Z]{3,9}\.?\s+)?[A-Z0-9].*$', '', title).strip()


def performance_location(lines, date_index):
    venue_index = None
    for index in range(date_index + 1, min(date_index + 7, len(lines))):
        candidate = lines[index]
        if re.search(
            r'\b(?:church|cathedral|chapel|synagogue|temple|hall|center|centre|theatre|theater)\b',
            candidate,
            re.IGNORECASE,
        ):
            venue_index = index
            break
    if venue_index is None:
        return '', ''
    venue = lines[venue_index]
    location = lines[venue_index + 1] if venue_index + 1 < len(lines) else ''
    return venue, location


def parse_event_page(soup, url, years):
    main = soup.select_one('main') or soup.select_one('#page')
    if not main or not years:
        return []
    title = page_title(soup)
    description = clean_text(main.get_text('\n', strip=True)) or None
    lines = [clean_text(line) for line in main.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    records = []
    for index, line in enumerate(lines):
        match = PERFORMANCE_RE.search(line)
        if not match:
            continue
        month_text, day, time_value, meridiem = match.groups()
        try:
            month = datetime.strptime(month_text.rstrip('.')[:3], '%b').month
            year = years[0] if month >= 7 else years[1]
            event_date = datetime(year, month, int(day)).date().isoformat()
            time_from = parse_time(time_value, meridiem)
        except ValueError:
            continue

        venue, location = performance_location(lines, index)
        city = infer_city(venue, location)
        if not title or not venue or not city:
            log_message(
                'Skipping performance with incomplete location',
                event='crawler_record_skipped',
                level='warning',
                url=url,
                venue=venue,
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    event_urls = set()
    for season_url in discover_seasons(session):
        season_soup = get_soup(session, season_url)
        years = season_years(season_soup, season_url)
        if not years:
            log_message(
                'Could not determine concert season years',
                event='crawler_season_skipped',
                level='warning',
                url=season_url,
            )
            continue
        for event_url in discover_event_urls(season_soup, season_url):
            key = (event_url, years)
            if key in event_urls:
                continue
            event_urls.add(key)
            records.extend(parse_event_page(get_soup(session, event_url), event_url, years))

    if not records:
        log_message(
            'No SFEMS concert performances found',
            event='crawler_empty_listing',
            level='warning',
            url=CURRENT_SEASON_URL,
            record_count=0,
        )
    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))


class SfemsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfems_org',
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
    SfemsOrgCrawler().run()


if __name__ == '__main__':
    main()
