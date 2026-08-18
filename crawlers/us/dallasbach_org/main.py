import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dallasbach.org/'
SOURCE = 'Dallas Bach Society'
CITY = 'Dallas'

LISTING_URLS = (
    f'{SOURCE_URL}masterworks-series',
    f'{SOURCE_URL}aldredge-house-series',
    f'{SOURCE_URL}chambertickets',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*'
    r'([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*,\s*(\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value).replace('S aturday', 'Saturday'))
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
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


def event_container(date_heading):
    """Return the smallest Wix container holding the event title and body."""
    node = date_heading
    while node:
        node = node.parent
        if not node or node.name == 'body':
            return None
        if node.find('h6') and node.find('p'):
            return node
    return None


def parse_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []

    for date_heading in soup.select('h1'):
        heading_text = clean_text(date_heading)
        event_date = parse_date(heading_text)
        if not event_date:
            continue

        container = event_container(date_heading)
        if not container:
            continue
        title_node = container.find('h6')
        title = clean_text(title_node)
        if not title:
            continue

        parts = [part.strip() for part in re.split(r'\s*~\s*|\n', heading_text) if part.strip()]
        venue = ''
        for part in parts[1:]:
            candidate = clean_text(TIME_RE.sub('', part)).strip(' ,-')
            if candidate:
                venue = candidate
        if not venue:
            # Aldredge events consistently name the venue in the dated heading;
            # this fallback also handles a line break after the time on Wix.
            for candidate in ('Our Redeemer Lutheran Church', 'The Meyerson Symphony Center',
                              'Meyerson Symphony Center', 'Aldredge House'):
                if candidate.lower() in heading_text.lower():
                    venue = candidate
                    break
        if not venue:
            continue

        paragraphs = []
        for paragraph in container.find_all('p'):
            text = clean_text(paragraph)
            if text and text not in paragraphs:
                paragraphs.append(text)
        description = '\n\n'.join(paragraphs) or None
        time_from = parse_time(heading_text)
        if time_from is None and 'aldredge-house-series' in url:
            # The series introduction and event copy establish a 7pm concert;
            # earlier times in the copy are hospitality, not performance times.
            time_from = '19:00'

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


def parse_chamber_archive(html, url):
    """Parse the older chamber-series page, whose dates are paragraph text."""
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for title_node in soup.select('h5'):
        title = clean_text(title_node)
        if not title or title == 'Chamber Series Tickets':
            continue
        section = title_node.find_parent('section')
        if not section:
            continue
        text = clean_text(section)
        event_date = parse_date(text)
        date_match = DATE_RE.search(text)
        venue_match = re.search(
            r'~\s*([^~]+?)\s+(?:\d{1,2}(?::\d{2})?\s*[ap]\.?(?:m\.?)?)',
            text,
            re.IGNORECASE,
        )
        concert_time = re.search(
            r'(\d{1,2}(?::\d{2})?\s*[ap]\.?(?:m\.?)?)\s+Concert\b',
            text,
            re.IGNORECASE,
        )
        if not event_date or not date_match or not venue_match:
            continue
        venue = clean_text(venue_match.group(1)).strip(' ,-')
        if not venue:
            continue
        body = text[date_match.end():].strip(' ~')
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(concert_time.group(1)) if concert_time else None,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': body or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for url in LISTING_URLS:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            if url.endswith('/chambertickets'):
                page_records = parse_chamber_archive(response.text, url)
            else:
                page_records = parse_page(response.text, url)
            records.extend(page_records)
            if not page_records:
                log_message(
                    'No concert entries found on listing page',
                    event='crawler_empty_listing',
                    level='warning',
                    url=url,
                    record_count=0,
                )
        except requests.RequestException as error:
            log_message(
                'Concert listing request failed',
                event='crawler_request_failed',
                level='error',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['title']))


class DallasBachOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dallasbach_org',
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
    DallasBachOrgCrawler().run()


if __name__ == '__main__':
    main()
