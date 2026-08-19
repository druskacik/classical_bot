import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mnbach.org/'
SOURCE = 'Minnesota Bach Ensemble'
DEFAULT_CITY = 'St Paul'
DEFAULT_VENUE = 'Sundin Music Hall at Hamline University'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,?\s+(?P<year>\d{4}))?\s+(?:at\s+)?'
    r'(?P<time>\d{1,2}(?::?\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'(?P<start>20\d{2})\s*[-–]\s*(?P<end>\d{2,4})')
ARCHIVE_PATH_RE = re.compile(r'^/20\d{2}-20\d{2}-concerts/?$')
NON_EVENT_PATHS = {'/season-pass', '/back-to-bach-project'}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def season_years(text):
    match = SEASON_RE.search(text)
    if not match:
        return None
    start = int(match.group('start'))
    raw_end = match.group('end')
    end = int(raw_end) if len(raw_end) == 4 else (start // 100) * 100 + int(raw_end)
    return start, end


def infer_year(month, season):
    if not season:
        return None
    month_number = datetime.strptime(month[:3], '%b').month
    return season[0] if month_number >= 7 else season[1]


def parse_time(value):
    normalized = re.sub(r'\.', '', value).replace(' ', '').upper()
    if re.fullmatch(r'\d{3,4}(?:AM|PM)', normalized):
        meridiem = normalized[-2:]
        digits = normalized[:-2].zfill(4)
        normalized = f'{digits[:-2]}:{digits[-2:]}{meridiem}'
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_links(soup, base_url):
    links = []
    for anchor in soup.select('a[href]'):
        url = urljoin(base_url, anchor.get('href')).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc not in {'www.mnbach.org', 'mnbach.org'}:
            continue
        path = parsed.path.rstrip('/') or '/'
        if path in NON_EVENT_PATHS or path == '/':
            continue
        if 'concert' in path and not ARCHIVE_PATH_RE.fullmatch(path):
            links.append(url)
        elif clean_text(anchor.get_text(' ', strip=True)).lower() in {'learn more'}:
            links.append(url)
    return list(dict.fromkeys(links))


def page_content(soup):
    page = soup.select_one('[id^="page-"]') or soup.select_one('main') or soup
    return page


def parse_detail(soup, url, fallback_season=None):
    content = page_content(soup)
    text = clean_text(content.get_text(' ', strip=True))
    season = season_years(text) or fallback_season
    title = clean_text((soup.title.get_text() if soup.title else '').split('—')[0])
    if not title or title.lower() in {'home', 'minnesota bach ensemble'}:
        heading = content.find(['h1', 'h2'])
        title = clean_text(heading.get_text(' ', strip=True)) if heading else ''

    location_match = re.search(
        r'(Sundin Music Hall at Hamline University|Antonello Hall,?\s*MacPhail Center for Music)',
        text,
        re.IGNORECASE,
    )
    if location_match and location_match.group(1).lower().startswith('antonello'):
        venue, city = 'Antonello Hall, MacPhail Center for Music', 'Minneapolis'
    elif location_match:
        venue, city = DEFAULT_VENUE, DEFAULT_CITY
    else:
        return []

    records = []
    for match in DATE_RE.finditer(text):
        year = int(match.group('year')) if match.group('year') else infer_year(match.group('month'), season)
        if not year:
            continue
        try:
            event_date = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
            ).date().isoformat()
        except ValueError:
            continue
        time_from = parse_time(match.group('time'))
        if not time_from:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    home_response = session.get(SOURCE_URL, timeout=45)
    home_response.raise_for_status()
    home_soup = BeautifulSoup(home_response.text, 'html.parser')
    home_season = season_years(clean_text(home_soup.get_text(' ', strip=True)))
    home_detail_urls = event_links(home_soup, SOURCE_URL)

    sitemap_response = session.get(urljoin(SOURCE_URL, 'sitemap.xml'), timeout=45)
    sitemap_response.raise_for_status()
    sitemap = BeautifulSoup(sitemap_response.text, 'xml')
    sitemap_urls = {
        clean_text(loc.get_text()).rstrip('/') for loc in sitemap.find_all('loc')
    }
    detail_pages = [
        (url, home_season) for url in home_detail_urls
        if url.rstrip('/') in sitemap_urls
    ]
    archive_urls = []
    for loc in sitemap.find_all('loc'):
        url = clean_text(loc.get_text())
        if ARCHIVE_PATH_RE.fullmatch(urlparse(url).path):
            archive_urls.append(url)

    for archive_url in archive_urls:
        response = session.get(archive_url, timeout=45)
        response.raise_for_status()
        archive_soup = BeautifulSoup(response.text, 'html.parser')
        archive_season = season_years(clean_text(archive_soup.get_text(' ', strip=True)))
        detail_pages.extend(
            (url, archive_season) for url in event_links(archive_soup, archive_url)
        )

    records = []
    seen_pages = set()
    for url, fallback_season in detail_pages:
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_detail(BeautifulSoup(response.text, 'html.parser'), url, fallback_season))
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (item['url'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))
    if not result:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class MnBachOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mnbach_org',
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
    MnBachOrgCrawler().run()


if __name__ == '__main__':
    main()
