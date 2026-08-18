import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jacksonsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-and-events/')
INDEX_URLS = (
    LISTING_URL,
    urljoin(SOURCE_URL, 'classical-concert-series/'),
    urljoin(SOURCE_URL, 'music-on-tap/'),
    urljoin(SOURCE_URL, 'special-events/'),
)
SOURCE = 'Jackson Symphony Orchestra'
DEFAULT_CITY = 'Jackson'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            'JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
            'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER',
        ),
        start=1,
    )
}
MONTH_PATTERN = '|'.join(MONTHS)
DATE_LINE_RE = re.compile(rf'\b(?:{MONTH_PATTERN})\b.+?\b\d{{4}}\b', re.I)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?(?=\b|\s|$)', re.I)
EVENT_PATH_RE = re.compile(r'^/concerts-and-events/[^/]+/$')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_dates(value):
    """Expand the date forms used by the site's event headings."""
    text = clean_text(value).upper().replace('–', '-').replace('—', '-')
    year_match = re.search(r'\b(20\d{2})\b', text)
    if not year_match:
        return []
    year = int(year_match.group(1))
    before_year = text[:year_match.start()]
    before_year = TIME_RE.sub('', before_year)
    month_matches = list(re.finditer(rf'\b({MONTH_PATTERN})\b', before_year))
    parsed = []

    for index, month_match in enumerate(month_matches):
        month = MONTHS[month_match.group(1)]
        end = month_matches[index + 1].start() if index + 1 < len(month_matches) else len(before_year)
        day_text = before_year[month_match.end():end]
        day_text = re.sub(r'(\d)(?:ST|ND|RD|TH)\b', r'\1', day_text)
        for start_text, end_text in re.findall(r'\b(\d{1,2})(?:\s*-\s*(\d{1,2}))?\b', day_text):
            start_day = int(start_text)
            end_day = int(end_text or start_text)
            if end_day < start_day:
                continue
            for day in range(start_day, end_day + 1):
                try:
                    value = date(year, month, day).isoformat()
                except ValueError:
                    continue
                if value not in parsed:
                    parsed.append(value)
    return parsed


def parse_times(value):
    times = []
    for hour_text, minute_text, meridiem in TIME_RE.findall(clean_text(value)):
        hour = int(hour_text)
        if not 1 <= hour <= 12:
            continue
        minute = int(minute_text or '00')
        if minute > 59:
            continue
        hour = hour % 12 + (12 if meridiem.upper() == 'P' else 0)
        parsed = f'{hour:02d}:{minute:02d}'
        if parsed not in times:
            times.append(parsed)
    return times


def inline_location(value):
    text = clean_text(value).replace('–', '-').replace('—', '-')
    year_match = re.search(r'\b20\d{2}\b', text)
    if not year_match:
        return '', DEFAULT_CITY
    tail = text[year_match.end():].strip()
    tail = re.sub(r'^\s*[-,:]\s*', '', tail)
    tail = re.split(
        r'(?:^|\s)(?:@|AT)\s+\d{1,2}(?::\d{2})?\s*[AP]\.?(?:M\.?)?',
        tail,
        1,
        flags=re.I,
    )[0]
    tail = clean_text(tail)
    if not tail:
        return '', DEFAULT_CITY

    city = DEFAULT_CITY
    city_match = re.search(r'\s+IN\s+([A-Za-z][A-Za-z .\'-]+)$', tail, re.I)
    if city_match:
        city = clean_text(city_match.group(1)).title()
        tail = clean_text(tail[:city_match.start()])
    return tail.title(), city


def detail_description(content, header_wrapper):
    parts = []
    for node in content.select('p, h2, h3, h5, h6, li'):
        if header_wrapper in node.parents:
            continue
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event_page(html, url, fallback_text=''):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('#content')
    title_node = content.select_one('h1') if content else None
    if not content or not title_node:
        return []

    title = clean_text(title_node)
    header_wrapper = title_node.parent
    headings = [clean_text(node) for node in header_wrapper.select('h4')]
    fallback_year = re.search(r'\b(20\d{2})\b', fallback_text)
    dateish_re = re.compile(rf'\b(?:{MONTH_PATTERN})\b.+?\b\d{{1,2}}\b', re.I)
    date_headings = []
    for heading in headings:
        if DATE_LINE_RE.search(heading):
            date_headings.append(heading)
        elif fallback_year and dateish_re.search(heading):
            date_headings.append(f'{heading}, {fallback_year.group(1)}')
    standalone_times = [
        time_from
        for heading in headings
        if not dateish_re.search(heading)
        for time_from in parse_times(heading)
    ]
    common_venues = [
        heading
        for heading in headings
        if heading and not dateish_re.search(heading) and not parse_times(heading)
    ]
    common_venue = common_venues[-1].title() if common_venues else ''
    description = detail_description(content, header_wrapper)
    records = []

    for heading in date_headings:
        dates = parse_dates(heading)
        times = parse_times(heading) or standalone_times or [None]
        venue, city = inline_location(heading)
        venue = venue or common_venue
        if not title or not dates or not venue:
            continue
        for event_date in dates:
            for time_from in times:
                records.append({
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
                })
    return records


def listing_entries(html):
    soup = BeautifulSoup(html, 'html.parser')
    entries = {}
    for link in soup.select('a[href]'):
        url = urljoin(LISTING_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc == 'www.jacksonsymphony.org' and EVENT_PATH_RE.fullmatch(parsed.path):
            if parsed.path != '/concerts-and-events/concert-series/':
                normalized_url = f'{parsed.scheme}://{parsed.netloc}{parsed.path}'
                row = next(
                    (
                        parent
                        for parent in link.parents
                        if parent.name == 'div'
                        and 'vc_row' in (parent.get('class') or [])
                        and DATE_LINE_RE.search(clean_text(parent))
                    ),
                    None,
                )
                entries.setdefault(normalized_url, clean_text(row) if row else '')
    return entries


def listing_urls(html):
    return sorted(listing_entries(html))


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    entries = {}
    for index_url in INDEX_URLS:
        response = session.get(index_url, timeout=45)
        response.raise_for_status()
        for url, fallback_text in listing_entries(response.text).items():
            if url not in entries or (not entries[url] and fallback_text):
                entries[url] = fallback_text

    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(session.get, url, timeout=45): url for url in entries}
        for future in as_completed(futures):
            url = futures[future]
            try:
                detail_response = future.result()
                detail_response.raise_for_status()
                records.extend(parse_event_page(detail_response.text, url, entries[url]))
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    unique_records = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique_records.setdefault(key, record)
    return sorted(
        unique_records.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class JacksonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jacksonsymphony_org',
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
    JacksonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
