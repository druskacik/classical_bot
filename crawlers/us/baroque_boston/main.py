import re
from datetime import date, datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://baroque.boston/'
SOURCE = 'Boston Baroque'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH = (
    r'January|February|March|April|May|June|July|August|September|October|'
    r'November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'
)
DATE_RE = re.compile(
    rf'\b(?P<month>{MONTH})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?'
    rf'(?:\s*,?\s*(?P<year>20\d{{2}}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?(?=\b|\s)', re.I)
STATE_RE = re.compile(r',\s*([A-Za-z .\'-]+),\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b')


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'P':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_date(match, fallback_year):
    year = int(match.group('year') or fallback_year)
    try:
        return datetime.strptime(
            f"{match.group('month')[:3]} {match.group('day')} {year}", '%b %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def extract_location(text):
    location = re.search(
        r'\bLocation\b\s*\n(?P<venue>[^\n|]{3,120})(?:\s*\n)?\s*\|\s*'
        r'(?P<address>[^\n]{3,180})',
        text,
        re.IGNORECASE,
    )
    if location:
        venue = location.group('venue').strip(' |')
        address = location.group('address').strip()
        city_match = STATE_RE.search(address)
        if venue and city_match:
            return venue, city_match.group(1).strip()

    # Older archived pages use a compact layout rather than labelled fields.
    known_venues = {
        'Jordan Hall at NEC': 'Boston',
        "New England Conservatory’s Jordan Hall": 'Boston',
        "New England Conservatory's Jordan Hall": 'Boston',
        "NEC’s Jordan Hall": 'Boston',
        "NEC's Jordan Hall": 'Boston',
        'GBH Calderwood Studio': 'Boston',
        'Boston Conservatory at Berklee, Seully Hall': 'Boston',
    }
    for venue, city in known_venues.items():
        if venue.lower() in text.lower():
            return venue, city
    return None


def parse_page(html, url, fallback_year):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    text = clean_text(main)
    location = extract_location(text)
    if not text or not location:
        return []

    title_tag = soup.select_one('meta[property="og:title"]')
    title = title_tag.get('content', '').strip() if title_tag else ''
    title = re.sub(r'\s+(?:—|-)\s+Boston Baroque.*$', '', title).strip()
    if not title:
        return []

    venue, city = location
    records = []
    seen = set()
    season_match = re.search(r'/(?:bb)?(\d{2})(\d{2})(?:season|-)', urlparse(url).path, re.I)
    for match in DATE_RE.finditer(text):
        # Dates in the biography/programme body are not occurrence evidence.
        context = text[max(0, match.start() - 55):match.end() + 55]
        if not (TIME_RE.search(context) or re.search(r'Dates?/Times?|Concerts? begin', context, re.I)):
            continue
        event_date = parse_date(match, fallback_year)
        if season_match and not match.group('year'):
            month_number = datetime.strptime(match.group('month')[:3], '%b').month
            season_year = 2000 + int(season_match.group(1))
            event_date = parse_date(match, season_year if month_number >= 7 else season_year + 1)
        if not event_date:
            continue
        # The closest time after the date belongs to that occurrence; using a
        # time before it can accidentally borrow the preceding performance.
        time_suffix = text[match.end():match.end() + 20]
        if not re.match(r'^\s*(?:(?:[/|]|at)\s*)?\d', time_suffix, re.I):
            continue
        time_from = parse_time(time_suffix)
        if time_from is None:
            continue
        key = (event_date, time_from, venue)
        if key in seen:
            continue
        seen.add(key)
        records.append({
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
        })
    return records


class BaroqueBostonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='baroque_boston',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SITEMAP_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Boston Baroque sitemap',
                event='crawler_fetch_failed', level='error', url=SITEMAP_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        sitemap = BeautifulSoup(response.content, 'xml')
        pages = []
        for node in sitemap.find_all('url'):
            loc = node.find('loc')
            if loc is None:
                continue
            url = loc.get_text(strip=True)
            parsed = urlparse(url)
            if parsed.netloc != 'baroque.boston' or parsed.path.startswith('/merchandise'):
                continue
            if re.search(r'(?:season|masterclass)', parsed.path, re.I):
                continue
            modified = node.find('lastmod')
            fallback_year = date.today().year
            if modified:
                year_match = re.match(r'(20\d{2})', modified.get_text(strip=True))
                if year_match:
                    fallback_year = int(year_match.group(1))
            explicit_year = re.search(r'(?<!\d)(20\d{2})(?!\d)', parsed.path)
            if explicit_year:
                fallback_year = int(explicit_year.group(1))
            pages.append((url, fallback_year))

        records = []
        for url, fallback_year in pages:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Boston Baroque page',
                    event='crawler_page_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            records.extend(parse_page(response.text, url, fallback_year))

        unique = {}
        for row in records:
            key = (row['title'], row['date'], row['time_from'], row['venue'])
            unique[key] = row
        return sorted(
            unique.values(), key=lambda row: (row['date'], row['time_from'] or '', row['title'])
        )


def main():
    BaroqueBostonCrawler().run()


if __name__ == '__main__':
    main()
