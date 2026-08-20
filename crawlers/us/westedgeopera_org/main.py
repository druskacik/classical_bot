import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.westedgeopera.org/'
SOURCE = 'West Edge Opera'
COUNTRY_CODE = 'US'
DEFAULT_VENUE = 'Oakland Scottish Rite Center'
DEFAULT_CITY = 'Oakland'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

EXCLUDED_PATHS = {
    '', 'about', 'audition-portal', 'board-team', 'brochure2026', 'calendar',
    'cart', 'commissions', 'company', 'diversity', 'donate', 'legacy-circle',
    'new-works', 'photos-1', 'press-room-1', 'pressreleases', 'stories',
    'support', 'venue',
    '2026-operas',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?'
    r'\s*(?:[•|]\s*|at\s+)(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)
DATE_WITHOUT_WEEKDAY_RE = re.compile(
    r'(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?'
    r'\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_urls(session):
    soup = get_soup(session, SOURCE_URL)
    urls = set()
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc.lower() not in {'westedgeopera.org', 'www.westedgeopera.org'}:
            continue
        path = parsed.path.strip('/')
        if path and path.lower() not in EXCLUDED_PATHS and '/' not in path:
            urls.add(f'{parsed.scheme}://{parsed.netloc}{parsed.path}')
    return sorted(urls)


def page_title(soup):
    node = soup.select_one('meta[property="og:title"]')
    title = clean_text(node.get('content')) if node else ''
    if title:
        return re.split(r'\s+(?:—|\|)\s+', title, maxsplit=1)[0].strip()
    return ''


def venue_and_city(text):
    if re.search(r'Wattis Theater|SFMOMA', text, re.IGNORECASE):
        return 'Phyllis Wattis Theater at SFMOMA', 'San Francisco'
    return DEFAULT_VENUE, DEFAULT_CITY


def parse_event_page(soup, url):
    main = soup.select_one('main')
    if not main:
        return []
    description = clean_text(main.get_text('\n', strip=True))
    title = page_title(soup)
    if not title or not description:
        return []

    whole_page = clean_text(soup.get_text(' ', strip=True))
    year_match = re.search(r'\b(20\d{2})\s+Operas\b', whole_page)
    if not year_match:
        return []
    year = int(year_match.group(1))
    matches = list(DATE_RE.finditer(description))
    if not matches:
        matches = list(DATE_WITHOUT_WEEKDAY_RE.finditer(description))
    venue, city = venue_and_city(description)

    records = []
    seen = set()
    for match in matches:
        try:
            starts_at = datetime.strptime(
                f'{year} {match.group("month")} {match.group("day")} '
                f'{match.group("time").replace(" ", "")}',
                '%Y %B %d %I%p',
            )
        except ValueError:
            continue
        key = (starts_at.date(), starts_at.time())
        if key in seen:
            continue
        seen.add(key)
        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': url,
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in event_urls(session):
        try:
            records.extend(parse_event_page(get_soup(session, url), url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['url']),
    )


class WestedgeoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='westedgeopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    WestedgeoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
