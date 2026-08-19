import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.northbrooksymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Northbrook Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'january|february|march|april|may|june|july|august|'
    'september|october|november|december'
)
SLUG_DATE_RE = re.compile(rf'(?:^|-)(?:{MONTHS})-\d{{1,2}}-\d{{4}}$', re.I)
PAGE_DATE_RE = re.compile(
    rf'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*'
    rf'({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})'
    rf'(?:\s*[·|]\s*(\d{{1,2}}(?::\d{{2}})?\s*[ap]m))?',
    re.I,
)
LOCATION_RE = re.compile(r'^(.+?)\s*\(([^()]*(?:,|\bIL\b)[^()]*)\)\s*$')


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date_and_time(value):
    match = PAGE_DATE_RE.search(clean_text(value))
    if not match:
        return '', None

    month, day, year, time_value = match.groups()
    try:
        event_date = datetime.strptime(
            f'{month} {day} {year}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return '', None

    time_from = None
    if time_value:
        normalized = clean_text(time_value).upper()
        for pattern in ('%I:%M%p', '%I%p', '%I:%M %p', '%I %p'):
            try:
                time_from = datetime.strptime(normalized, pattern).strftime('%H:%M')
                break
            except ValueError:
                continue
    return event_date, time_from


def event_urls_from_sitemap(content):
    soup = BeautifulSoup(content, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.split('/') if part]
        if (
            parsed.netloc == 'www.northbrooksymphony.org'
            and len(path_parts) == 1
            and SLUG_DATE_RE.search(path_parts[0])
        ):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_location(value):
    match = LOCATION_RE.match(clean_text(value))
    if not match:
        return '', ''
    venue, address = match.groups()
    address_parts = [clean_text(part) for part in address.split(',')]
    city = address_parts[-1] if len(address_parts) > 1 else ''
    city = re.sub(r'\s+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$', '', city).strip()
    return clean_text(venue), city


def parse_event_page(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    main = soup.select_one('main') or soup
    headings = main.select('h1, h2, h3, h4')
    h1_values = [clean_text(node.get_text(' ', strip=True)) for node in main.select('h1')]
    h1_values = [value for value in h1_values if value]

    date = ''
    time_from = None
    date_text = ''
    for value in h1_values:
        date, time_from = parse_date_and_time(value)
        if date:
            date_text = value
            break

    title = next((value for value in h1_values if value != date_text), '')
    venue = ''
    city = ''
    location_text = ''
    for node in headings:
        value = clean_text(node.get_text(' ', strip=True))
        parsed_venue, parsed_city = parse_location(value)
        if parsed_venue and parsed_city:
            venue, city, location_text = parsed_venue, parsed_city, value
            break

    if not all((title, date, venue, city)):
        log_message(
            'Skipping event page with incomplete required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(date),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    description_parts = []
    for node in main.select('h3, h4, p'):
        value = clean_text(node.get_text(' ', strip=True))
        if (
            value
            and value not in {date_text, location_text}
            and value not in description_parts
            and 'reserves the right to record and photograph' not in value.lower()
        ):
            description_parts.append(value)

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    urls = event_urls_from_sitemap(response.content)

    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_event_page(response.content, url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to retrieve event page',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concert event pages found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class NorthbrookSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='northbrooksymphony_org',
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

    def scrape(self):
        return scrape_concerts()


def main():
    NorthbrookSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
