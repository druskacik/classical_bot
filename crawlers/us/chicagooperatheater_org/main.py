import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chicagooperatheater.org/'
SOURCE = 'Chicago Opera Theater'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
REPERTOIRE_URL = f'{SOURCE_URL}past-repertoire'
CITY = 'Chicago'
DEFAULT_VENUE = 'Studebaker Theater at the Fine Arts Building'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7,
    'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'sept': 9,
    'september': 9, 'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}
DATE_TOKEN_RE = re.compile(
    r'\b(' + '|'.join(MONTHS) + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b', re.I
)
YEAR_RE = re.compile(r'\b(20\d{2})\b')
TIME_RE = re.compile(r'@\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.I)
SEASON_RE = re.compile(r'^(20\d{2})\s*[-–/]\s*(\d{2,4})$')
EXCLUDED_PATH_PARTS = {
    'blog', 'photo', 'preview', 'program', 'review', 'membership', 'history',
    'playlist', 'closeup', 'artist', 'staff', 'board', 'donate', 'support',
}
STOPWORDS = {'a', 'an', 'and', 'at', 'beyond', 'concert', 'der', 'discovery', 'of', 'the'}


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def parse_occurrences(value, season_start=None):
    """Parse dates sharing a trailing year, as used throughout COT pages."""
    text = clean_text(value)
    tokens = list(DATE_TOKEN_RE.finditer(text))
    if not tokens:
        return []
    years = [(match.start(), int(match.group(1))) for match in YEAR_RE.finditer(text)]
    occurrences = []
    for index, token in enumerate(tokens):
        boundary = tokens[index + 1].start() if index + 1 < len(tokens) else len(text)
        nearby_years = [year for position, year in years if token.start() <= position <= boundary + 30]
        month = MONTHS[token.group(1).lower()]
        year = nearby_years[0] if nearby_years else None
        if year is None and season_start:
            year = season_start if month >= 8 else season_start + 1
        if year is None and years:
            year = years[-1][1]
        if year is None:
            continue
        try:
            date = datetime(year, month, int(token.group(2))).date().isoformat()
        except ValueError:
            continue
        segment_start = token.start()
        segment_end = boundary
        occurrences.append((date, parse_time(text[segment_start:segment_end])))
    return occurrences


def title_tokens(value):
    return {
        token for token in re.findall(r'[a-z0-9]+', value.lower())
        if token not in STOPWORDS and len(token) > 1
    }


def repertoire_entries(soup):
    entries = []
    season_start = None
    main = soup.select_one('main') or soup
    for node in main.select('h2, h3, p'):
        text = clean_text(node)
        season_match = SEASON_RE.fullmatch(text)
        if season_match:
            season_start = int(season_match.group(1))
            continue
        if season_start is None or node.name != 'p':
            continue
        for line in node.get_text('\n', strip=True).splitlines():
            name = re.sub(r'\s*[+*^]+\s*$', '', clean_text(line))
            name = re.sub(r'\s*\([^)]*\)\s*$', '', name)
            if name and len(title_tokens(name)):
                entries.append((name, season_start))
    return entries


def sitemap_urls(soup):
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node)
        parsed = urlparse(url)
        if parsed.netloc == 'www.chicagooperatheater.org' and not re.search(r'\.[a-z0-9]{2,5}$', parsed.path, re.I):
            urls.append(url)
    return urls


def match_archive_pages(entries, urls):
    matches = []
    used = set()
    for title, season_start in entries:
        wanted = title_tokens(title)
        best = None
        for url in urls:
            path = urlparse(url).path.strip('/').lower()
            path_words = set(re.findall(r'[a-z0-9]+', path))
            if not path or path_words & EXCLUDED_PATH_PARTS:
                continue
            present = title_tokens(path.replace('-', ' '))
            overlap = len(wanted & present)
            required = 1 if len(wanted) == 1 else min(2, len(wanted))
            if overlap >= required and (best is None or overlap > best[0]):
                best = (overlap, url)
        if best and best[1] not in used:
            used.add(best[1])
            matches.append((title, season_start, best[1]))
    return matches


def venue_from_text(text):
    for line in text.splitlines():
        cleaned = clean_text(line)
        if 'studebaker theater' in cleaned.lower():
            return DEFAULT_VENUE
        if any(word in cleaned.lower() for word in ('theater', 'theatre', 'auditorium')):
            value = re.split(r'\s*[|•]\s*|\s+\d{2,5}\s', cleaned)[0].strip()
            if value and len(value) <= 120:
                return value
    return None


def page_description(soup):
    parts = []
    for block in soup.select('main .sqs-html-content'):
        text = clean_text(block)
        if len(text) >= 80 and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def records_from_overview(soup, url):
    records = []
    for item in soup.select('li.accordion-item'):
        heading = item.select_one('.accordion-item__title-wrapper')
        title = clean_text(heading)
        text = item.get_text('\n', strip=True)
        venue = venue_from_text(text)
        if not title or not venue:
            continue
        description = clean_text(item)
        for date, time_from in parse_occurrences(text):
            records.append(make_record(title, date, time_from, venue, description, url))
    return records


def records_from_archive_page(soup, title, season_start, url):
    main = soup.select_one('main') or soup
    text = main.get_text('\n', strip=True)
    venue = venue_from_text(text)
    if not venue:
        return []
    # Performance dates precede the first venue line. Later prose often contains
    # premiere/history dates that are not occurrences.
    venue_match = re.search(r'(?i)studebaker theater|theatre|auditorium', text)
    header_text = text[:venue_match.start()] if venue_match else text
    occurrences = parse_occurrences(header_text, season_start)
    description = page_description(soup)
    return [make_record(title, date, time, venue, description, url) for date, time in occurrences]


def make_record(title, date, time_from, venue, description, url):
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'xml' if url.endswith('.xml') else 'html.parser')


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    sitemap = get_soup(session, SITEMAP_URL)
    urls = sitemap_urls(sitemap)
    repertoire = get_soup(session, REPERTOIRE_URL)
    entries = repertoire_entries(repertoire)

    records = []
    season_urls = [url for url in urls if re.search(r'/20\d{2}-\d{2}-season$', url)]
    for url in season_urls:
        try:
            records.extend(records_from_overview(get_soup(session, url), url))
        except requests.RequestException as error:
            log_message('Season page request failed', event='crawler_request_failed', level='warning',
                        url=url, error_type=type(error).__name__, error_message=str(error))

    for title, season_start, url in match_archive_pages(entries, urls):
        try:
            records.extend(records_from_archive_page(get_soup(session, url), title, season_start, url))
        except requests.RequestException as error:
            log_message('Archive page request failed', event='crawler_request_failed', level='warning',
                        url=url, error_type=type(error).__name__, error_message=str(error))

    unique = {}
    for record in records:
        key = (record['title'].lower(), record['date'], record['venue'].lower())
        existing = unique.get(key)
        if existing is None or (existing['time_from'] is None and record['time_from'] is not None):
            unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['time_from'] or ''))
    if not result:
        log_message('No concrete performances found', event='crawler_empty_listing', level='warning',
                    url=SOURCE_URL, record_count=0)
    return result


class ChicagoOperaTheaterOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chicagooperatheater_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=['title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
                 'description', 'source_url', 'source'],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChicagoOperaTheaterOrgCrawler().run()


if __name__ == '__main__':
    main()
