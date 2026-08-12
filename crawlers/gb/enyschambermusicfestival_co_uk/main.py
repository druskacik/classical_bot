import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.enyschambermusicfestival.co.uk/'
SITEMAP_URL = urljoin(SOURCE_URL, 'pages-sitemap.xml')
SOURCE = 'Enys Chamber Music Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|November|December'
)
DATE_RE = re.compile(
    rf'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*'
    rf'(?:({MONTHS})\s+(\d{{1,2}})|(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})))'
    rf'(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)
YEAR_PAGE_RE = re.compile(r'/(20\d{2})(?:-?festival)$', re.IGNORECASE)

VENUES = {
    'enys house': ('Enys House', 'Penryn'),
    'enys gardens': ('Enys Gardens', 'Penryn'),
    'penryn town hall': ('Penryn Town Hall', 'Penryn'),
    'the poly': ('The Poly', 'Falmouth'),
    'all saints church': ('All Saints Church', 'Falmouth'),
    'king charles the martyr': ('King Charles the Martyr', 'Falmouth'),
    'st michael and all angels': ('St Michael and All Angels', 'Falmouth'),
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def festival_pages(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    pages = []
    for node in soup.select('url > loc'):
        url = clean_text(node)
        match = YEAR_PAGE_RE.search(url.rstrip('/'))
        if match:
            pages.append((url, int(match.group(1))))
    return sorted(set(pages), key=lambda item: item[1], reverse=True)


def parse_date(text, year):
    match = DATE_RE.search(text)
    if not match:
        return None
    month = match.group(1) or match.group(4)
    day = match.group(2) or match.group(3)
    event_year = match.group(5) or str(year)
    try:
        return datetime.strptime(f'{day} {month} {event_year}', '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def event_section(heading):
    section = heading.find_parent('section')
    if section and section.select_one('a[href]'):
        return section
    return None


def venue_and_city(lines, date_line):
    try:
        date_index = lines.index(date_line)
    except ValueError:
        return None, None
    candidates = [line for line in lines[1:date_index] if len(line) <= 100]
    for candidate in reversed(candidates):
        lower = candidate.lower()
        for venue_key, (venue, city) in VENUES.items():
            if venue_key in lower:
                return venue, city
    return None, None


def detail_description(session, url):
    try:
        soup = BeautifulSoup(get_response(session, url).content, 'html.parser')
    except requests.RequestException as error:
        log_message(
            'Failed to scrape Enys festival event detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    main = soup.select_one('main') or soup.body
    if not main:
        return None
    parts = []
    for node in main.select('h1, h2, h3, h4, p'):
        text = clean_text(node)
        if not text or text in parts:
            continue
        if text.lower() in {'top of page', 'bottom of page', 'meet the musicians'}:
            continue
        parts.append(text)
    description = '\n\n'.join(parts)
    return description or None


def parse_festival_page(session, content, page_url, year):
    soup = BeautifulSoup(content, 'html.parser')
    records = []
    for heading in soup.select('h3'):
        section = event_section(heading)
        if not section:
            continue
        title = clean_text(heading)
        lines = list(dict.fromkeys(clean_text(node) for node in section.select('h3, p') if clean_text(node)))
        date_line = next((line for line in lines if DATE_RE.search(line)), None)
        event_date = parse_date(date_line or '', year)
        venue, city = venue_and_city(lines, date_line) if date_line else (None, None)
        link = section.select_one('a[href]')
        url = urljoin(page_url, link.get('href')) if link else None
        if not all((title, event_date, url, venue, city)):
            continue
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(date_line),
                'venue': venue,
                'city': city,
                'country_code': 'GB',
                'description': detail_description(session, url),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page_url, year in festival_pages(session):
        try:
            response = get_response(session, page_url)
            records.extend(parse_festival_page(session, response.content, page_url, year))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Enys festival programme page',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(records, key=lambda record: (record['date'], record['time_from'] or '', record['title']))


class EnysChamberMusicFestivalCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='enyschambermusicfestival_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    EnysChamberMusicFestivalCrawler().run()


if __name__ == '__main__':
    main()
