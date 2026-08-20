import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.yellowbarn.org/'
SOURCE = 'Yellow Barn'
SEARCH_URL = urljoin(SOURCE_URL, 'search/node/{query}')
DISCOVERY_TERMS = ('concert', 'recital', 'performance')
MAX_WORKERS = 8

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(\d{4}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.IGNORECASE)
EXCLUDED_TITLE_RE = re.compile(
    r'\b(?:masterclass|pre[- ]concert|discussion|conversation|patio noise|lecture|talk|workshop)\b',
    re.IGNORECASE,
)
PERFORMANCE_TITLE_RE = re.compile(
    r'\b(?:concert|recital|performance|opening night|season finale|composer portrait|special event|opera)\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value, fallback_year):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year = match.groups()
    year = year or fallback_year
    if not year:
        return None
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(f'{month} {day} {year}', '%b %d %Y').date().isoformat()
        except ValueError:
            return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour)
    if not 1 <= hour <= 12:
        return None
    if meridiem.lower() == 'p' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'a' and hour == 12:
        hour = 0
    return f'{hour:02d}:{int(minute or 0):02d}'


def parse_location(value):
    parts = [part.strip() for part in clean_text(value).split(',') if part.strip()]
    if len(parts) < 2:
        return None, None
    venue = parts[0]
    # Some touring listings use a street address as their first component.
    # An address is not a defensible venue name, so omit those occurrences.
    if re.match(r'^\d+\s', venue):
        return None, None
    if len(parts) >= 3 and re.fullmatch(r'[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?', parts[-1]):
        city = parts[-2]
    else:
        city = re.sub(r'\s+[A-Z]{2}$', '', parts[1]).strip()
    if not venue or not city or venue.casefold() == city.casefold():
        return None, None
    return venue, city


def page_year(soup, url):
    heading = soup.select_one('.past-event-heading')
    candidates = [clean_text(heading), clean_text(soup.select_one('h1')), url]
    for candidate in candidates:
        match = re.search(r'\b(19\d{2}|20\d{2})\b', candidate)
        if match:
            return match.group(1)
    return None


def field_text(node, field_name):
    return clean_text(node.select_one(f'.field-name-{field_name}'))


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    fallback_year = page_year(soup, url)
    records = []

    for node in soup.select('.node-event'):
        title = clean_text(node.select_one('.node-title'))
        date_text = field_text(node, 'field-date')
        location_text = field_text(node, 'field-location')
        program = field_text(node, 'field-program')
        body = field_text(node, 'body')
        composers = field_text(node, 'field-composers')

        # Yellow Barn event-series pages also contain talks and masterclasses.
        # A published programme is the strongest first-party performance marker;
        # familiar performance labels cover older entries without that field.
        if not title or EXCLUDED_TITLE_RE.search(title):
            continue
        if not program and not PERFORMANCE_TITLE_RE.search(title):
            continue

        event_date = parse_date(date_text, fallback_year)
        venue, city = parse_location(location_text)
        if not event_date or not venue or not city:
            continue

        description_parts = []
        for text in (body, composers, program):
            if text and text not in description_parts:
                description_parts.append(text)

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(date_text),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': '\n\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_html(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def search_page_urls(term):
    first_url = SEARCH_URL.format(query=quote(term))
    first_html = get_html(first_url)
    first_soup = BeautifulSoup(first_html, 'html.parser')
    page_numbers = [0]
    for link in first_soup.select('.pager a[href*="page="]'):
        match = re.search(r'[?&]page=(\d+)', link.get('href', ''))
        if match:
            page_numbers.append(int(match.group(1)))

    search_urls = [first_url]
    last_page = max(page_numbers)
    search_urls.extend(f'{first_url}?page={page}' for page in range(1, last_page + 1))

    html_pages = [first_html]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(get_html, url) for url in search_urls[1:]]
        for future in as_completed(futures):
            try:
                html_pages.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Yellow Barn search page request failed',
                    event='crawler_search_page_failed',
                    level='warning',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    event_urls = set()
    for html in html_pages:
        soup = BeautifulSoup(html, 'html.parser')
        for link in soup.select('ol.search-results a[href], .search-results a[href]'):
            candidate = urljoin(SOURCE_URL, link.get('href'))
            if candidate.startswith(urljoin(SOURCE_URL, 'events/')):
                event_urls.add(candidate.split('#', 1)[0])
    return event_urls


def discover_event_urls():
    urls = set()
    for term in DISCOVERY_TERMS:
        urls.update(search_page_urls(term))
    return sorted(urls)


class YellowBarnOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='yellowbarn_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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

    def __init__(self, page_urls=None):
        self.page_urls = page_urls

    def scrape(self):
        event_urls = sorted(self.page_urls) if self.page_urls is not None else discover_event_urls()
        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(get_html, url): url for url in event_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_event_page(future.result(), url))
                except requests.RequestException as error:
                    log_message(
                        'Yellow Barn event page request failed',
                        event='crawler_event_page_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        if not records:
            log_message(
                'No Yellow Barn concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    YellowBarnOrgCrawler().run()


if __name__ == '__main__':
    main()
