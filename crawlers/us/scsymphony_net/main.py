import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.scsymphony.net/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
ARCHIVE_URL = urljoin(SOURCE_URL, 'past-concert-archive')
SOURCE = 'Southern Crescent Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

SEASON_RE = re.compile(r'(20\s*\d\s*\d)\s*[-/]\s*(20\s*\d\s*\d)')
DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*)?'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(AM|PM)(?!\w)', re.IGNORECASE)
VENUE_WORD_RE = re.compile(
    r'\b(?:hall|church|school|theatre|theater|auditorium|center|centre|chapel|arena)\b',
    re.IGNORECASE,
)

VENUE_CITIES = {
    'Spivey Hall': 'Morrow',
    'First Baptist Church of Morrow': 'Morrow',
    'Fayette County High School': 'Fayetteville',
}


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def season_years(soup):
    for heading in soup.select('h1, h2, h3'):
        match = SEASON_RE.search(clean_text(heading.get_text(' ', strip=True)))
        if match:
            return tuple(int(re.sub(r'\s+', '', value)) for value in match.groups())
    return None


def parse_date(match, years):
    year = years[0] if datetime.strptime(match.group('month'), '%B').month >= 7 else years[1]
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'PM':
        hour += 12
    return f"{hour:02d}:{int(match.group(2) or 0):02d}"


def canonical_venue(text):
    normalized = clean_text(text).strip(' /,-')
    for venue in VENUE_CITIES:
        if venue.lower() in normalized.lower():
            return venue
    if normalized and len(normalized) <= 100 and VENUE_WORD_RE.search(normalized):
        return normalized
    return None


def event_from_section(heading, page_url, years):
    section = heading.find_parent('div', class_='oKdM2c')
    if not section:
        return None

    paragraphs = [clean_text(node.get_text(' ', strip=True)) for node in section.select('p')]
    paragraphs = [value for value in paragraphs if value]
    date_index = next((index for index, value in enumerate(paragraphs) if DATE_RE.search(value)), None)
    if date_index is None:
        return None

    date_match = DATE_RE.search(paragraphs[date_index])
    event_date = parse_date(date_match, years)
    nearby = paragraphs[date_index:date_index + 3]
    time_from = parse_time(' '.join(nearby))

    venue = None
    date_line = paragraphs[date_index]
    if '/' in date_line:
        venue = canonical_venue(date_line.rsplit('/', 1)[-1])
    if not venue:
        for value in nearby:
            venue = canonical_venue(value)
            if venue:
                break
    if not event_date or not venue:
        return None

    city = VENUE_CITIES.get(venue)
    if not city:
        city_match = re.search(r'\b(?:of|in)\s+([A-Z][A-Za-z .-]+)$', venue)
        city = clean_text(city_match.group(1)) if city_match else None
    if not city:
        return None

    title = clean_text(heading.get_text(' ', strip=True))
    description = [
        value for index, value in enumerate(paragraphs)
        if index != date_index and value != venue and not TIME_RE.fullmatch(value)
    ]
    fragment = heading.get('id')
    url = f'{page_url}#{fragment}' if fragment else page_url
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def discover_pages(session):
    pages = {CONCERTS_URL}
    for index_url in (SOURCE_URL, CONCERTS_URL, ARCHIVE_URL):
        response = session.get(index_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.select('a[href]'):
            url = urljoin(index_url, link.get('href')).split('#', 1)[0]
            path = urlparse(url).path.rstrip('/')
            if path.startswith('/past-concert-archive/'):
                pages.add(url)
    return sorted(pages)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for page_url in discover_pages(session):
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            years = season_years(soup)
            if not years:
                log_message(
                    'Concert page has no parseable season',
                    event='crawler_page_skipped',
                    level='warning',
                    url=page_url,
                )
                continue
            for heading in soup.select('h1'):
                record = event_from_section(heading, page_url, years)
                if record:
                    records.append(record)
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_request_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['venue']))


class ScsymphonyNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='scsymphony_net',
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
        return scrape_concerts()


def main():
    ScsymphonyNetCrawler().run()


if __name__ == '__main__':
    main()
