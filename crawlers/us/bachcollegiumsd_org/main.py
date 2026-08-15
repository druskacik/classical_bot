import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bachcollegiumsd.org/'
SOURCE = 'Bach Collegium San Diego'
SITEMAP_URL = 'https://bachcollegiumsd.squarespace.com/sitemap.xml'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Za-z]+\s+\d{1,2},?\s+20\d{2}),?\s*'
    r'(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)$', re.I
)
DATE_ONLY_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Za-z]+\s+\d{1,2},?\s+20\d{2})$', re.I
)
RANGE_RE = re.compile(r'^[A-Za-z]+\s+\d{1,2}(?:-\d{1,2})?,\s+20\d{2}$')
SHORT_DATE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Za-z]+\s+\d{1,2}),?\s*(?:at\s+)?(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)?', re.I
)

VENUE_CITIES = {
    'The Conrad Prebys Performing Arts Center': 'La Jolla',
    "All Souls' Episcopal Church": 'San Diego',
    'All Souls’ Episcopal Church': 'San Diego',
    'First United Methodist Church': 'San Diego',
    'St. Augustine By-the-Sea Episcopal Church': 'Santa Monica',
    'Saints Constantine and Helen Greek Orthodox Church': 'Cardiff',
    'Church of the Nazarene in Mid-City': 'San Diego',
    'Verbatim Books': 'San Diego',
    'Vi at La Jolla Village': 'San Diego',
    'San Diego Public Library, San Ysidro Branch': 'San Diego',
}


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200d', '')
    return re.sub(r'[ \t]+', ' ', text).strip()


def parse_date(value):
    value = clean_text(value).replace(',', '')
    try:
        return datetime.strptime(value, '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def lines_from(node):
    return [clean_text(line) for line in node.get_text('\n', strip=True).splitlines() if clean_text(line)]


def city_for(venue, following_lines):
    for line in following_lines[:3]:
        match = re.search(r',\s*([^,]+),\s*CA(?:\s+\d{5})?\b', line, re.I)
        if match:
            return clean_text(match.group(1))
    return VENUE_CITIES.get(venue)


def make_record(title, event_date, event_time, venue, city, url, description):
    if not all((title, event_date, venue, city, url)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_mainstage(soup, page_url):
    main = soup.select_one('main')
    if not main:
        return []
    lines = lines_from(main)
    starts = [index for index, line in enumerate(lines) if RANGE_RE.match(line)]
    detail_urls = []
    for link in main.find_all('a', href=True):
        if 'DETAIL' in clean_text(link.get_text()).upper():
            url = urljoin(page_url, link['href'])
            if url not in detail_urls:
                detail_urls.append(url)
    records = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        parts = lines[start + 1:end]
        title = next(
            (
                line for line in parts
                if line.upper() == line and line != 'SEASON HIGHLIGHT'
                and not DATE_TIME_RE.match(line) and 'DETAIL' not in line
            ),
            '',
        )
        detail_url = detail_urls[position] if position < len(detail_urls) else page_url
        description = '\n'.join(parts) or None
        for index, line in enumerate(parts):
            match = DATE_TIME_RE.match(line)
            if not match:
                continue
            event_date = parse_date(match.group(1))
            event_time = parse_time(match.group(2))
            venue = parts[index + 1] if index + 1 < len(parts) else ''
            city = city_for(venue, parts[index + 2:index + 5])
            record = make_record(title, event_date, event_time, venue, city, detail_url, description)
            if record:
                records.append(record)
    return records


def parse_noon(soup, page_url):
    main = soup.select_one('main')
    lines = lines_from(main) if main else []
    starts = [index for index, line in enumerate(lines) if DATE_ONLY_RE.match(line) or DATE_TIME_RE.match(line)]
    records = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        chunk = lines[start:end]
        date_match = DATE_ONLY_RE.match(chunk[0]) or DATE_TIME_RE.match(chunk[0])
        event_date = parse_date(date_match.group(1))
        event_time = parse_time(date_match.group(2)) if date_match.lastindex == 2 else None
        cursor = 1
        if not event_time and cursor < len(chunk):
            event_time = parse_time(chunk[cursor])
            if event_time:
                cursor += 1
        # Current pages put the venue before the title; archived pages put it after the programme.
        venue_index = next((i for i in range(cursor, len(chunk)) if chunk[i] in VENUE_CITIES), None)
        title_index = next((i for i in range(cursor, len(chunk)) if chunk[i].isupper() and len(chunk[i]) > 3), None)
        if venue_index is None or title_index is None:
            continue
        venue = chunk[venue_index]
        title = chunk[title_index]
        city = city_for(venue, chunk[venue_index + 1:venue_index + 4])
        record = make_record(title, event_date, event_time, venue, city, page_url, '\n'.join(chunk[title_index + 1:]))
        if record:
            records.append(record)

        giving_index = next((i for i, line in enumerate(chunk) if line == 'GivingBACH Free Community Performance'), None)
        if giving_index is not None:
            secondary_date_index = next(
                (i for i in range(giving_index + 1, len(chunk)) if SHORT_DATE_RE.match(chunk[i])), None
            )
            if secondary_date_index is not None:
                secondary_match = SHORT_DATE_RE.match(chunk[secondary_date_index])
                year = event_date[:4]
                secondary_date = parse_date(f'{secondary_match.group(1)} {year}')
                secondary_time = parse_time(secondary_match.group(2)) if secondary_match.group(2) else None
                secondary_venue_index = next(
                    (i for i in range(secondary_date_index + 1, len(chunk)) if chunk[i] in VENUE_CITIES), None
                )
                if secondary_venue_index is not None:
                    secondary_venue = chunk[secondary_venue_index]
                    secondary_city = city_for(
                        secondary_venue, chunk[secondary_venue_index + 1:secondary_venue_index + 4]
                    )
                    secondary = make_record(
                        f'{title} — GivingBACH Free Community Performance',
                        secondary_date, secondary_time, secondary_venue, secondary_city,
                        page_url, '\n'.join(chunk[title_index + 1:]),
                    )
                    if secondary:
                        records.append(secondary)
    return records


def discover_pages(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    pages = set()
    for location in soup.find_all('loc'):
        url = clean_text(location.get_text())
        path = url.rstrip('/').rsplit('/', 1)[-1].lower()
        if re.fullmatch(r'mainstage-concerts-overview-\d{2}-\d{2}', path):
            pages.add(url)
        elif re.fullmatch(r'bachatnoon(?:-\d+)?', path):
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
            if 'mainstage-concerts-overview-' in page_url.lower():
                records.extend(parse_mainstage(soup, page_url))
            else:
                records.extend(parse_noon(soup, page_url))
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    if not records:
        log_message(
            'No concerts found', event='crawler_empty_listing', level='warning',
            url=SOURCE_URL, record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BachCollegiumSdOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachcollegiumsd_org',
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
    BachCollegiumSdOrgCrawler().run()


if __name__ == '__main__':
    main()
