import re
from collections import Counter
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bachsocietymn.org/'
SITEMAP_URL = urljoin(SOURCE_URL, 'pages-sitemap.xml')
SOURCE = 'Bach Society of Minnesota'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|'
    'October|November|December'
)
WEEKDAYS = 'Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday'
DATE_LINE_RE = re.compile(
    rf'^({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?'
    rf'(?:\s+(20\d{{2}}))?,?\s+({WEEKDAYS})\b(.*)$',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?)\s*([AP]M)\b', re.IGNORECASE)
ADDRESS_CITY_RE = re.compile(
    r',\s*([A-Za-z][A-Za-z .\'()\-]*?)(?:,?\s+MN)?(?:\s+\d{5}(?:-\d{4})?)?\s*$'
)
VENUE_ADDRESS_RE = re.compile(r'^(.+?)\s+\d{1,6}\s+\S.*?,\s*[^,]+$')
NON_EVENT_PATH_RE = re.compile(r'(?:^|/)(?:\d{4}-\d{2}-season|minnesota-bach-festival-overview\d*)/?$')

# This retained page omits the year, but its weekday/date pair belongs to the
# archived 2025-26 season. Keep the inference tied to the exact first-party URL.
YEAR_OVERRIDES = {
    '/solo-cello': 2025,
    '/2026kickoffparty': 2026,
    '/bachtothefuture': 2026,
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_line(value, url):
    value = clean_text(value)
    match = DATE_LINE_RE.match(value)
    if not match:
        return None
    month, day, year, weekday, remainder = match.groups()
    if not year:
        year = YEAR_OVERRIDES.get(urlparse(url).path.rstrip('/'))
    if not year:
        return None
    try:
        date_value = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date()
    except ValueError:
        return None
    if date_value.strftime('%A').lower() != weekday.lower():
        return None
    range_match = re.search(
        r'\b(\d{1,2}(?::\d{2})?)\s*(?:([AP]M)\s*)?[-–]\s*'
        r'\d{1,2}(?::\d{2})?\s*([AP]M)\b',
        remainder,
        re.IGNORECASE,
    )
    time_match = TIME_RE.search(remainder)
    time_from = None
    if range_match:
        raw_time = f'{range_match.group(1)}{range_match.group(2) or range_match.group(3)}'.upper()
    elif time_match:
        raw_time = ''.join(time_match.groups()).upper()
    else:
        raw_time = ''
    if raw_time:
        for pattern in ('%I:%M%p', '%I%p'):
            try:
                time_from = datetime.strptime(raw_time, pattern).strftime('%H:%M')
                break
            except ValueError:
                pass
    return date_value.isoformat(), time_from


def page_title(soup):
    main = soup.select_one('main')
    if main:
        headings = [
            clean_text(heading.get_text(' ', strip=True))
            for heading in main.select('h1, h2, h3, h4, h5')
        ]
        headings = [heading for heading in headings if heading]
        if headings:
            counts = Counter(headings)
            return max(headings, key=lambda heading: counts[heading])
    meta = soup.select_one('meta[property="og:title"][content]')
    title = clean_text(meta.get('content') if meta else '')
    if not title and soup.title:
        title = clean_text(soup.title.get_text(' ', strip=True))
    return re.split(r'\s*[|–-]\s*Bach Society', title, maxsplit=1)[0].strip()


def location_from_box(date_node):
    box = date_node.find_parent('div', class_=lambda value: value and 'wixui-box' in value)
    if not box:
        return None, None
    lines = [clean_text(node.get_text(' ', strip=True)) for node in box.select('p')]
    lines = [line for line in lines if line]
    date_text = clean_text(date_node.get_text(' ', strip=True))
    try:
        date_index = lines.index(date_text)
    except ValueError:
        return None, None

    candidates = lines[date_index + 1 : date_index + 9]
    address_index = None
    location = ''
    for index, line in enumerate(candidates):
        candidate = re.split(r'\s+Tickets?:', line, maxsplit=1, flags=re.IGNORECASE)[0]
        if ADDRESS_CITY_RE.search(candidate):
            address_index = index
            location = candidate
            break
    if address_index is None:
        return None, None
    city_match = ADDRESS_CITY_RE.search(location)
    city = clean_text(city_match.group(1))
    parenthetical = re.search(r'\(([A-Za-z][A-Za-z .\'-]+)\)\s*$', city)
    if parenthetical:
        city = clean_text(parenthetical.group(1))

    venue = candidates[address_index - 1] if address_index else location
    if address_index == 0 or venue.lower().startswith(('ticket', 'part of')):
        venue_match = VENUE_ADDRESS_RE.match(location)
        if not venue_match:
            return None, None
        venue = clean_text(venue_match.group(1))
    venue = re.sub(r'^\([^)]*\)\s*', '', venue).strip()
    return venue, city


def description_from_page(soup):
    main = soup.select_one('main')
    if not main:
        return None
    for node in main.select('nav, script, style, noscript'):
        node.decompose()
    return clean_text(main.get_text('\n', strip=True)) or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    title = page_title(soup)
    if not main or not title:
        return []
    description = description_from_page(soup)
    records = []
    for node in main.select('p'):
        parsed = parse_date_line(node.get_text(' ', strip=True), url)
        if not parsed:
            continue
        venue, city = location_from_box(node)
        if not venue or not city:
            continue
        event_date, time_from = parsed
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def sitemap_urls(html):
    soup = BeautifulSoup(html, 'xml')
    source_host = urlparse(SOURCE_URL).netloc
    return sorted(
        {
            clean_text(node.get_text())
            for node in soup.select('loc')
            if urlparse(clean_text(node.get_text())).netloc == source_host
            and not NON_EVENT_PATH_RE.search(urlparse(clean_text(node.get_text())).path)
        }
    )


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()

    records = []
    failed = 0
    for url in sitemap_urls(response.text):
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            records.extend(parse_detail(detail_response.text, url))
        except requests.RequestException as error:
            failed += 1
            log_message(
                'Failed to fetch sitemap page',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if failed:
        log_message(
            'Some sitemap pages could not be fetched',
            event='crawler_pages_failed',
            level='warning',
            url=SITEMAP_URL,
            record_count=failed,
        )
    if not records:
        log_message(
            'No usable concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class BachsocietymnOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachsocietymn_org',
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
    BachsocietymnOrgCrawler().run()


if __name__ == '__main__':
    main()
