import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://victoriabachfestival.org/'
ARCHIVE_URL = f'{SOURCE_URL}concerts/past-performances/'
SOURCE = 'Victoria Bach Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(2), '%B %d, %Y').date()
    except ValueError:
        return None


def parse_dates(value):
    text = clean_text(value)
    matches = list(DATE_RE.finditer(text))
    if not matches:
        return []
    first = parse_date(matches[0].group(0))
    if not first:
        return []
    if len(matches) < 2:
        return [first.isoformat()]
    last = parse_date(matches[1].group(0))
    if not last or last < first or (last - first).days > 31:
        return [first.isoformat()]
    return [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    compact = re.sub(r'\s+', ' ', match.group(1).strip().upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(compact, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_urls(session):
    response = session.get(ARCHIVE_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    container = soup.select_one('.em-events-list-grouped')
    if not container:
        return []
    return list(dict.fromkeys(
        link['href'] for link in container.select('a[href*="/events/"]')
        if '/events/categories/' not in link['href']
    ))


def labelled_block(soup, label):
    for strong in soup.find_all('strong'):
        if clean_text(strong.get_text(' ', strip=True)).casefold() == label.casefold():
            return strong.find_parent(class_=lambda value: value and 'fusion-text' in value)
    return None


def description_from_page(soup, date_block):
    event = soup.select_one('.em-event-single')
    if not event:
        return None
    parts = []
    for block in event.select('.fusion-text'):
        if block is date_block:
            break
        text = clean_text(block.get_text('\n', strip=True))
        if not text or text.casefold() == 'event details':
            continue
        if re.fullmatch(r'(purchase|buy) tickets?.*', text, re.IGNORECASE):
            continue
        if text not in parts:
            parts.append(text)
    description = '\n\n'.join(parts)
    if description:
        return description
    # Some server-side cache variants omit the visual description block but
    # retain the event's first-party Open Graph summary.
    meta = soup.select_one('meta[property="og:description"]')
    return clean_text(meta.get('content')) if meta else None


def parse_event(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        title_node = soup.select_one('h1')
        date_block = labelled_block(soup, 'Date/Time')
        location_block = labelled_block(soup, 'Location')
        if not title_node or not date_block or not location_block:
            return []

        title = clean_text(title_node.get_text(' ', strip=True))
        dates = parse_dates(date_block.get_text(' ', strip=True))
        time_from = parse_time(date_block.get_text(' ', strip=True))
        location_lines = [
            clean_text(line) for line in location_block.get_text('\n', strip=True).splitlines()
        ]
        location_lines = [line for line in location_lines if line and line.casefold() != 'location']
        venue_link = location_block.select_one('a[href*="/locations/"]')
        venue = clean_text(venue_link.get_text(' ', strip=True)) if venue_link else ''
        city = ''
        for line in reversed(location_lines):
            match = re.fullmatch(r'([^,]+),\s*(?:Texas|TX)(?:\s+\d{5})?', line, re.IGNORECASE)
            if match:
                city = clean_text(match.group(1))
                break

        if not title or not dates or not venue or not city:
            return []
        description = description_from_page(soup, date_block)
        return [{
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
        } for event_date in dates]
    except requests.RequestException as error:
        log_message(
            'Event request failed',
            event='crawler_event_request_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    if not urls:
        log_message(
            'No archived events found',
            event='crawler_empty_listing',
            level='warning',
            url=ARCHIVE_URL,
            record_count=0,
        )
        return []

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(parse_event, session, url) for url in urls]
        for future in as_completed(futures):
            records.extend(future.result())
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class VictoriaBachFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='victoriabachfestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    VictoriaBachFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
