import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fcmtx.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Friends of Chamber Music Bryan-College Station'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
DATE_RE = re.compile(
    rf'(?:(?:Mon|Tue(?:s)?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)(?:day)?\s*,?\s*)?'
    rf'({MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,|\s+)\s*(20\d{{2}})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?(?:\b|$)', re.I)

# The organization presents concerts in the adjacent cities of Bryan and College
# Station.  These first-party venue names provide the necessary city evidence.
VENUE_CITIES = {
    'A&M United Methodist Church': 'College Station',
    'Annenberg Presidential Conference Center': 'College Station',
    'Annenberg Presidential Center': 'College Station',
    'George H.W. Bush Presidential Library & Museum': 'College Station',
    'George Bush Presidential Library': 'College Station',
    'Rudder Theatre': 'College Station',
    'Rudder Auditorium': 'College Station',
    'Century Square': 'College Station',
    'St. Thomas Episcopal Church': 'College Station',
    'The Ice House': 'Bryan',
    'Ice House on Main': 'Bryan',
    'First Presbyterian Church': 'Bryan',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ').replace('\u200b', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month[:3]} {day} {year}', '%b %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def find_venue(value):
    normalized = clean_text(value).lower()
    for venue in sorted(VENUE_CITIES, key=len, reverse=True):
        if venue.lower() in normalized:
            return venue, VENUE_CITIES[venue]
    return None, None


def event_url(container, page_url):
    for link in container.select('a[href]'):
        href = urljoin(page_url, link.get('href'))
        if urlparse(href).scheme in {'http', 'https'} and href != SOURCE_URL:
            return href
    return page_url


def card_title(card, lines):
    candidates = []
    for node in card.select('h1, h2, h3, h4'):
        value = clean_text(node)
        if value and not DATE_RE.search(value) and not find_venue(value)[0]:
            if value.lower() not in {'learn more', 'buy tickets', 'free registration'}:
                candidates.append(value)
    candidates = list(dict.fromkeys(candidates))
    if candidates:
        return ' — '.join(candidates)

    date_index = next((i for i, line in enumerate(lines) if DATE_RE.search(line)), -1)
    venue_index = next((i for i, line in enumerate(lines) if find_venue(line)[0]), -1)
    indexes = range(0, date_index) if date_index > 0 else range(venue_index + 1, len(lines))
    for index in indexes:
        value = lines[index]
        if value and not DATE_RE.search(value) and not find_venue(value)[0]:
            return value
    return ''


def record_from_container(container, page_url):
    lines = [clean_text(item) for item in container.stripped_strings]
    lines = [item for item in lines if item and item != '\u200b']
    text = '\n'.join(lines)
    event_date = parse_date(text)
    venue, city = find_venue(text)
    title = card_title(container, lines)
    if not event_date or not venue or not city or not title:
        return None

    description_lines = [
        line for line in lines
        if line not in {title, venue}
        and not re.fullmatch(r'(?:buy tickets|free registration|registration closed|learn more)', line, re.I)
    ]
    return {
        'title': title,
        'date': event_date,
        'url': event_url(container, page_url),
        'time_from': parse_time(text),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n'.join(description_lines) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_repeater_cards(soup, page_url):
    records = []
    for card in soup.select('.wixui-repeater__item'):
        if not DATE_RE.search(clean_text(card)):
            continue
        record = record_from_container(card, page_url)
        if record:
            records.append(record)
    return records


def parse_legacy_page(soup, page_url):
    """Parse old Wix seasons whose events are plain date/venue/title blocks."""
    lines = [clean_text(item) for item in soup.get_text('\n', strip=True).splitlines()]
    lines = [item for item in lines if item]
    date_indexes = [index for index, line in enumerate(lines) if DATE_RE.search(line)]
    records = []
    for position, start in enumerate(date_indexes):
        end = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        block = lines[start:end]
        venue_index = next((i for i, line in enumerate(block[:8]) if find_venue(line)[0]), None)
        if venue_index is None:
            continue
        venue, city = find_venue(block[venue_index])
        title = next(
            (line for line in block[venue_index + 1:venue_index + 5]
             if line.lower() not in {'learn more', 'buy tickets'}),
            '',
        )
        event_date = parse_date(block[0])
        if not title or not event_date:
            continue
        description = '\n'.join(block[venue_index + 2:]) or None
        records.append({
            'title': title,
            'date': event_date,
            'url': page_url,
            'time_from': parse_time(block[0]),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def archive_urls(soup):
    urls = []
    for link in soup.select('a[href]'):
        href = urljoin(CONCERTS_URL, link.get('href'))
        if urlparse(href).netloc == 'www.fcmtx.org' and re.search(
            r'/(?:season-|copy-of-concerts|copy-of-season-|this-season)', href
        ):
            urls.append(href)
    return list(dict.fromkeys(urls))


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    listing_response = session.get(CONCERTS_URL, timeout=45)
    listing_response.raise_for_status()
    listing_soup = BeautifulSoup(listing_response.text, 'html.parser')
    pages = [SOURCE_URL, CONCERTS_URL, *archive_urls(listing_soup)]

    records = []
    for page_url in dict.fromkeys(pages):
        try:
            response = listing_response if page_url == CONCERTS_URL else session.get(page_url, timeout=45)
            response.raise_for_status()
            soup = listing_soup if page_url == CONCERTS_URL else BeautifulSoup(response.text, 'html.parser')
            page_records = parse_repeater_cards(soup, page_url)
            if not page_records:
                page_records = parse_legacy_page(soup, page_url)
            records.extend(page_records)
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title']))
    if not result:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return result


class FcmtxOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fcmtx_org',
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
    FcmtxOrgCrawler().run()


if __name__ == '__main__':
    main()
