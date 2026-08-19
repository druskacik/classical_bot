import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musiconthehillri.org/'
SOURCE = 'Music on the Hill'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})',
    re.IGNORECASE,
)
SHORT_DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*'
    r'([A-Z][a-z]+\s+\d{1,2}(?:,\s*|\s+)20\d{2})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9]):([0-5]\d)\s*([ap])\.?m\.?', re.IGNORECASE)
CITY_RE = re.compile(r'\b([A-Za-z][A-Za-z .\'-]+),\s*RI(?:\s+\d{5}(?:-\d{4})?)?\b')


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_RE.search(text) or SHORT_DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(re.sub(r'\s+', ' ', match.group(1)), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_location(text):
    lines = [line.strip(' ,') for line in text.splitlines() if line.strip(' ,')]
    city_match = next((CITY_RE.search(line) for line in lines if CITY_RE.search(line)), None)
    if city_match is None:
        city_match = CITY_RE.search(' '.join(lines))
    if not city_match or not lines:
        return None
    city = re.sub(r'^.*?\d{1,6}\s+', '', city_match.group(1)).strip()
    venue = lines[0]
    if not venue or venue.lower() == city.lower() or any(char.isdigit() for char in venue):
        return None
    return venue, city


def make_record(title, event_date, url, time_from, venue, city, description=None):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_detail_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if main is None:
        return None
    title = clean_text(main.select_one('h1'))
    headings = main.select('h2, h3')
    date_heading = next((heading for heading in headings if parse_date(clean_text(heading))), None)
    if not title or date_heading is None:
        return None
    date_text = clean_text(date_heading)
    event_date = parse_date(date_text)
    location = None
    for heading in headings[headings.index(date_heading) + 1:]:
        location = parse_location(clean_text(heading))
        if location:
            break
    if not event_date or not location:
        return None
    venue, city = location
    description = clean_text(main)
    return make_record(
        title, event_date, url, parse_time(date_text), venue, city, description
    )


def parse_concert_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    detail_urls = []
    for heading in soup.select('main h2'):
        link = heading.select_one('a[href]')
        if link is None:
            continue
        title = clean_text(link)
        date_node = heading.find_next_sibling('p')
        location_node = date_node.find_next_sibling('p') if date_node else None
        date_text = clean_text(date_node)
        location = parse_location(clean_text(location_node))
        event_date = parse_date(date_text)
        if not title or not event_date or not location:
            continue
        url = urljoin(SOURCE_URL, link['href'])
        venue, city = location
        records.append(make_record(title, event_date, url, parse_time(date_text), venue, city))
        detail_urls.append(url)

    # The festival page sometimes advertises an additional unlinked community concert.
    page_text = clean_text(soup.select_one('main'))
    community = re.search(
        r'community concert at\s+([^,\n—]+),\s*[^—\n]+—\s*'
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
        r'([A-Z][a-z]+\s+\d{1,2})\s+at\s+(\d{1,2}(?::\d{2})?\s*[AP]M)',
        page_text,
        re.IGNORECASE,
    )
    year_match = re.search(r'\b(20\d{2}) Music Festival\b', page_text)
    if community and year_match:
        event_date = parse_date(f'{community.group(2)}, {year_match.group(1)}')
        time_text = community.group(3)
        if ':' not in time_text:
            time_text = re.sub(r'(?i)\s*([AP]M)$', r':00 \1', time_text)
        if event_date:
            records.append(make_record(
                'Community Concert', event_date, f'{CONCERTS_URL}#community-concert',
                parse_time(time_text), community.group(1).strip(), 'Warwick',
                community.group(0).strip(),
            ))
    return records, detail_urls


class MusicOnTheHillRiOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musiconthehillri_org',
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
            response = session.get(CONCERTS_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Music on the Hill concert listing',
                event='crawler_fetch_failed', level='error', url=CONCERTS_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        listing_records, current_urls = parse_concert_listing(response.text)
        records_by_url = {record['url']: record for record in listing_records}

        try:
            sitemap_response = session.get(SITEMAP_URL, timeout=45)
            sitemap_response.raise_for_status()
            sitemap = BeautifulSoup(sitemap_response.content, 'xml')
            urls = [
                clean_text(loc) for loc in sitemap.select('url > loc')
                if urlparse(clean_text(loc)).netloc == urlparse(SOURCE_URL).netloc
            ]
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Music on the Hill sitemap archive',
                event='crawler_fetch_failed', level='warning', url=SITEMAP_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            urls = current_urls

        def fetch_detail(url):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                return parse_detail_page(detail_response.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Music on the Hill page',
                    event='crawler_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in set(urls)}
            for future in as_completed(futures):
                record = future.result()
                if record:
                    # The festival listing is authoritative when a detail page has a stale year.
                    if record['url'] in records_by_url:
                        records_by_url[record['url']]['description'] = record['description']
                    else:
                        records_by_url[record['url']] = record

        return sorted(
            records_by_url.values(),
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    MusicOnTheHillRiOrgCrawler().run()


if __name__ == '__main__':
    main()
