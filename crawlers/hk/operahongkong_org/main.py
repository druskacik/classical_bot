import html
import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operahongkong.org/'
SOURCE = 'Opera Hong Kong'
INDEX_URLS = (
    SOURCE_URL,
    urljoin(SOURCE_URL, 'upcoming-productions/'),
    urljoin(SOURCE_URL, 'past-productions/'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-HK,en;q=0.9',
}
MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3,
    'march': 3, 'apr': 4, 'april': 4, 'may': 5, 'jun': 6,
    'june': 6, 'jul': 7, 'july': 7, 'aug': 8, 'august': 8,
    'sep': 9, 'sept': 9, 'september': 9, 'oct': 10, 'october': 10,
    'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}
VENUE_WORDS = re.compile(
    r'\b(?:theatre|theater|hall|centre|center|auditorium|arena|studio)\b', re.I
)
NON_DETAIL_PATHS = {
    '/', '/upcoming-productions/', '/past-productions/', '/about-us/',
    '/education-and-outreach/', '/chorus/', '/contact-us/',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    else:
        value = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def detail_urls(session):
    urls = set()
    for index_url in INDEX_URLS:
        soup = get_soup(session, index_url)
        main = soup.select_one('main')
        if main is None:
            continue
        for link in main.select('a[href]'):
            url = urljoin(SOURCE_URL, link.get('href', '')).split('#', 1)[0]
            parsed = urlparse(url)
            if parsed.netloc.lower() not in {'operahongkong.org', 'www.operahongkong.org'}:
                continue
            path = parsed.path.rstrip('/') + '/'
            if path.startswith('/zh/') or path in NON_DETAIL_PATHS:
                continue
            urls.add(f'https://operahongkong.org{path}')
    return sorted(urls)


def parse_schedule(text):
    # Production pages put a compact all-caps schedule immediately before the
    # venue. Accept lists and inclusive ranges such as "15 & 16 AUG 2025" and
    # "8-11 OCT 2026".
    head = text[:2500]
    pattern = re.compile(
        r'(?P<days>\d{1,2}(?:\s*(?:-|–|—|&|,|and)\s*\d{1,2})*)\s+'
        r'(?P<month>JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|'
        r'JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?|SEP(?:T|TEMBER)?|OCT(?:OBER)?|'
        r'NOV(?:EMBER)?|DEC(?:EMBER)?)\s+(?P<year>20\d{2})',
        re.I,
    )
    match = pattern.search(head)
    if not match:
        return []
    raw_days = match.group('days')
    numbers = [int(value) for value in re.findall(r'\d{1,2}', raw_days)]
    if re.search(r'[-–—]', raw_days) and len(numbers) == 2:
        numbers = list(range(numbers[0], numbers[1] + 1))
    month = MONTHS[match.group('month').lower()]
    year = int(match.group('year'))
    dates = []
    for day in numbers:
        try:
            dates.append(date(year, month, day).isoformat())
        except ValueError:
            return []

    tail = head[match.end():]
    times = re.findall(r'(?<!\d)([012]?\d[:.]\d{2})(?!\d)', tail)
    times = [value.replace('.', ':').zfill(5) for value in times]
    if len(times) < len(dates):
        times.extend([None] * (len(dates) - len(times)))
    return list(zip(dates, times[:len(dates)]))


def parse_venue(text):
    lines = [line.strip(' \t|') for line in text[:3000].splitlines() if line.strip()]
    for line in lines:
        if '$' in line or len(line) > 140 or not VENUE_WORDS.search(line):
            continue
        # All accepted venue names must themselves establish Hong Kong. This
        # prevents applying the company's home city to a future tour listing.
        lowered = line.lower()
        if 'hong kong' in lowered or any(
            name in lowered for name in (
                'city hall', 'cultural centre', 'arts centre', 'fringe club',
                'jockey club auditorium', 'academy for performing arts',
            )
        ):
            return line, 'Hong Kong'
    return None


def parse_detail(soup, url):
    main = soup.select_one('main')
    if main is None:
        return []
    text = clean_text(main)
    schedule = parse_schedule(text)
    location = parse_venue(text)
    if not schedule or not location:
        return []

    heading = main.select_one('h1') or main.select_one('h2')
    title = clean_text(heading)
    if not title:
        title = clean_text(soup.title)
        title = re.sub(r'\s*[|\-–—]\s*Opera Hong Kong\s*$', '', title, flags=re.I)
    if not title:
        return []

    venue, city = location
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'HK',
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in schedule
    ]


class OperaHongKongOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operahongkong_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HK',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for url in detail_urls(session):
            try:
                records.extend(parse_detail(get_soup(session, url), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Opera Hong Kong production',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    OperaHongKongOrgCrawler().run()


if __name__ == '__main__':
    main()
