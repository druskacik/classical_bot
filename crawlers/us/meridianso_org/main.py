import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.meridianso.org/'
SOURCE = 'Meridian Symphony Association'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
CITY = 'Meridian'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

SEASON_PATH_RE = re.compile(r'^/(?:20\d{2}-\d{2}(?:-season)?|season)$')
DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:[,]?\s+(20\d{2}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(?:Performance\s+Time\s*:\s*)?(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b', re.I)
LOCATION_RE = re.compile(r'\bLocation\s*:\s*([^\n]+)', re.I)
PARENTHETICAL_CITY_RE = re.compile(r'\s*\(([A-Za-z .\'’ -]+),\s*[A-Z]{2}\)\s*$')


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '').replace('\u200d', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def sitemap_urls(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    child_sitemaps = [node.get_text(strip=True) for node in soup.select('sitemap > loc')]
    if child_sitemaps:
        urls = []
        for child_url in child_sitemaps:
            urls.extend(sitemap_urls(session, child_url))
        return urls
    return [node.get_text(strip=True) for node in soup.select('url > loc')]


def season_start_year(url, title):
    match = re.search(r'/(20\d{2})-\d{2}', url) or re.search(r'\b(20\d{2})\s*[-–]', title)
    return int(match.group(1)) if match else None


def inferred_year(month, season_years):
    if not season_years:
        return None
    # A reused detail URL can remain linked from a later season. Its oldest
    # season association is the reliable one (the newer link is stale).
    start = min(season_years)
    return start if month >= 7 else start + 1


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'PM' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def json_ld_events(soup):
    events = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict) and value.get('@type') == 'Event':
                events.append(value)
    return events


def parse_json_ld(event, url, description):
    try:
        start = datetime.fromisoformat(str(event.get('startDate')).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    location = event.get('location') or {}
    venue = clean_text(location.get('name'))
    if venue.lower() in {'', 'location is tbd', 'tbd'}:
        return None
    address = location.get('address') or {}
    city = clean_text(address.get('addressLocality')) if isinstance(address, dict) else ''
    city = city or CITY
    title = clean_text(BeautifulSoup(str(event.get('name') or ''), 'html.parser').get_text())
    if not title:
        return None
    return make_record(title, start.date().isoformat(), url, start.strftime('%H:%M'), venue, city, description)


def parse_html_event(soup, url, season_years):
    main = soup.select_one('main')
    if not main:
        return None
    description = clean_text(main.get_text('\n', strip=True), '\n')
    matches = list(DATE_RE.finditer(description))
    distinct_dates = {(m.group(1).lower(), m.group(2), m.group(3)) for m in matches}
    if not matches or len(distinct_dates) != 1:
        return None
    match = matches[0]
    month = datetime.strptime(match.group(1)[:3], '%b').month
    year = int(match.group(3)) if match.group(3) else inferred_year(month, season_years)
    if not year:
        return None
    try:
        date = datetime(year, month, int(match.group(2))).date().isoformat()
    except ValueError:
        return None

    location = LOCATION_RE.search(description)
    if not location:
        return None
    venue = clean_text(location.group(1)).split('\n', 1)[0]
    if venue.lower() in {'', 'location is tbd', 'tbd'}:
        return None
    title = clean_text((soup.title.get_text() if soup.title else '').split('|', 1)[0])
    if not title:
        return None
    city_match = PARENTHETICAL_CITY_RE.search(venue)
    city = clean_text(city_match.group(1)) if city_match else CITY
    if city_match:
        venue = venue[:city_match.start()].strip()
    return make_record(title, date, url, parse_time(description), venue, city, description)


def make_record(title, date, url, time_from, venue, city, description):
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=3, connect=3, read=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    session.mount('https://', HTTPAdapter(max_retries=retry))
    all_urls = sitemap_urls(session, SITEMAP_URL)
    page_urls = sorted({url for url in all_urls if urlparse(url).netloc == urlparse(SOURCE_URL).netloc})
    season_urls = [url for url in page_urls if SEASON_PATH_RE.fullmatch(urlparse(url).path)]

    candidate_years = {}
    for season_url in season_urls:
        response = session.get(season_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        year = season_start_year(season_url, soup.title.get_text(' ', strip=True) if soup.title else '')
        main = soup.select_one('main')
        if not main:
            continue
        for link in main.select('a[href]'):
            url = urljoin(season_url, link.get('href'))
            if urlparse(url).netloc == urlparse(SOURCE_URL).netloc and url != season_url:
                candidate_years.setdefault(url.split('#', 1)[0], set()).add(year)

    # Wix Events entries provide schema.org Event data and are an additional
    # first-party archive distinct from the hand-built season pages.
    for url in page_urls:
        if '/event-details-registration/' in url:
            candidate_years.setdefault(url, set())

    records = []
    skipped = 0
    for url, years in sorted(candidate_years.items()):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        main = soup.select_one('main')
        description = clean_text(main.get_text('\n', strip=True), '\n') if main else None
        parsed = [parse_json_ld(event, url, description) for event in json_ld_events(soup)]
        parsed = [record for record in parsed if record]
        if not parsed:
            record = parse_html_event(soup, url, {year for year in years if year})
            parsed = [record] if record else []
        if parsed:
            records.extend(parsed)
        else:
            skipped += 1

    if skipped:
        log_message(
            'Skipped candidate pages without one complete event occurrence',
            event='crawler_records_skipped',
            level='warning',
            url=SOURCE_URL,
            record_count=skipped,
        )
    if not records:
        log_message(
            'No valid event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MeridianSoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='meridianso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MeridianSoOrgCrawler().run()


if __name__ == '__main__':
    main()
