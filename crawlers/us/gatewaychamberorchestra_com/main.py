import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gatewaychamberorchestra.com/'
SITEMAP_URL = f'{SOURCE_URL}pages-sitemap.xml'
SOURCE = 'Gateway Chamber Orchestra'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(?P<day>\d{1,2}),\s*(?P<year>20\d{2})\s*\|?\s*'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))$',
    re.IGNORECASE,
)

CITY_RE = re.compile(r'\b(Clarksville|Nashville|Franklin)\b', re.IGNORECASE)
ADDRESS_RE = re.compile(r'^\d+\s')
BOILERPLATE_STARTS = ('Business', 'Gateway Chamber Orchestra')
ACTION_LINES = {
    'Purchase Tickets',
    'Individual Tickets',
    'Season Tickets',
    'Schedule',
    'Tickets:',
}


def clean_text(value):
    if not value:
        return ''
    value = str(value).replace('\xa0', ' ').replace('\u200b', ' ')
    return re.sub(r'\s+', ' ', value).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.fullmatch(clean_text(value))
    if not match:
        return None
    try:
        event_date = datetime.strptime(
            f"{match['month']} {match['day']} {match['year']}",
            '%B %d %Y',
        ).date().isoformat()
    except ValueError:
        return None

    raw_time = re.sub(r'\s*(AM|PM)$', r' \1', re.sub(r'\.', '', match['time']).upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            time_from = datetime.strptime(raw_time, pattern).strftime('%H:%M')
            return event_date, time_from
        except ValueError:
            continue
    return None


def page_lines(soup):
    return [
        text
        for text in (clean_text(line) for line in soup.get_text('\n').splitlines())
        if text
    ]


def page_title(soup):
    node = soup.select_one('meta[property="og:title"]')
    title = clean_text(node.get('content')) if node else ''
    if not title and soup.title:
        title = clean_text(soup.title.get_text(' ', strip=True))
    return re.sub(r'\s*\|\s*Gateway Chamber Orchestra\s*$', '', title).strip()


def description_from_lines(lines, start_index):
    parts = []
    for line in lines[start_index:]:
        if line.startswith(BOILERPLATE_STARTS):
            break
        if line in ACTION_LINES or line in parts:
            continue
        parts.append(line)
    return '\n'.join(parts) or None


def parse_event_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = page_title(soup)
    if not title:
        return []

    lines = page_lines(soup)
    records = []
    for index, line in enumerate(lines):
        parsed = parse_date_time(line)
        if not parsed or index + 1 >= len(lines):
            continue

        venue_line = lines[index + 1]
        city_match = CITY_RE.search(venue_line)
        if not city_match or ADDRESS_RE.match(venue_line):
            continue

        city = city_match.group(1).title()
        venue = re.sub(rf',\s*{re.escape(city)}\s*$', '', venue_line, flags=re.I).strip()
        if not venue or venue.casefold() == city.casefold():
            continue

        description_start = index + 2
        if description_start < len(lines) and ADDRESS_RE.match(lines[description_start]):
            description_start += 1

        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description_from_lines(lines, description_start),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    return [
        clean_text(node.get_text())
        for node in soup.find_all('loc')
        if clean_text(node.get_text()).startswith(SOURCE_URL)
    ]


def fetch_page(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = sitemap_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_page, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event_page(url, future.result()))
            except requests.RequestException as error:
                log_message(
                    'Event page request failed',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No concert occurrences with complete dates and venues found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class GatewayChamberOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gatewaychamberorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
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
    GatewayChamberOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
