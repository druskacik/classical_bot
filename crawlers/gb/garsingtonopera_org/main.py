import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://garsingtonopera.org/'
SOURCE = 'Garsington Opera'
STUDIOS_URL = f'{SOURCE_URL}studios-programme/'
HISTORY_URL = f'{SOURCE_URL}festival/performance-history/'
COUNTRY_CODE = 'GB'
DEFAULT_CITY = 'Stokenchurch'
FESTIVAL_VENUE = 'Opera Pavilion, Wormsley'
STUDIOS_VENUE = 'Garsington Studios'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(
            max_retries=Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=('GET',),
            )
        ),
    )
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def discovery_events(session):
    events = {}

    studios = get_soup(session, STUDIOS_URL)
    for link in studios.select('main a.c-col-card__link[href*="/whats-on/"]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        if urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
            continue
        if 'garsington-studios-open-day' in url:
            continue
        card = link.find_parent(class_=re.compile(r'c-col-card'))
        events[url] = {
            'year': None,
            'venue': STUDIOS_VENUE,
            'city': DEFAULT_CITY,
            'listing_date': clean_text(card) if card else '',
        }

    history = get_soup(session, HISTORY_URL)
    current_year = None
    for element in history.select('main h2, main a.c-event-card__permalink'):
        if element.name == 'h2':
            heading = clean_text(element)
            current_year = int(heading) if re.fullmatch(r'20\d{2}', heading) else current_year
            continue
        url = urljoin(SOURCE_URL, element.get('href'))
        if current_year and urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
            card = element.find_parent(class_=re.compile(r'c-event-card'))
            events[url] = {
                'year': current_year,
                'venue': FESTIVAL_VENUE,
                'city': DEFAULT_CITY,
                'listing_date': clean_text(
                    card.select_one('.c-event-card__daterange') if card else None
                ),
            }
    return events


def parse_date(value, default_year=None):
    value = clean_text(value)
    value = re.sub(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+', '', value)
    if default_year and not re.search(r'\b20\d{2}\b', value):
        value = f'{value} {default_year}'
    for pattern in ('%d %B %Y', '%d %b %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def event_dates(value, default_year=None):
    value = clean_text(value).replace('\u2013', '-').replace('\u2014', '-')
    single = parse_date(value, default_year)
    if single:
        return [single]

    match = re.fullmatch(
        r'(?:\w+\s+)?(\d{1,2})\s+([A-Za-z]+)\s*-\s*'
        r'(?:\w+\s+)?(\d{1,2})\s+([A-Za-z]+)(?:\s+(20\d{2}))?',
        value,
    )
    if not match:
        same_month = re.fullmatch(
            r'(?:\w+\s+)?(\d{1,2})\s*-\s*'
            r'(?:\w+\s+)?(\d{1,2})\s+([A-Za-z]+)(?:\s+(20\d{2}))?',
            value,
        )
        if not same_month:
            return []
        year = int(same_month.group(4) or default_year or 0)
        if not year:
            return []
        start = parse_date(f'{same_month.group(1)} {same_month.group(3)} {year}')
        end = parse_date(f'{same_month.group(2)} {same_month.group(3)} {year}')
        return list(dict.fromkeys(date for date in (start, end) if date))
    year = int(match.group(5) or default_year or 0)
    if not year:
        return []
    start = parse_date(f'{match.group(1)} {match.group(2)} {year}')
    end = parse_date(f'{match.group(3)} {match.group(4)} {year}')
    # The source publishes production ranges rather than every historical
    # performance. Both boundaries are advertised performance dates; do not
    # invent the intervening schedule.
    return list(dict.fromkeys(date for date in (start, end) if date))


def parse_time(value):
    value = value.lower().replace('.', ':')
    patterns = (
        r'concert start time\s*:\s*(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm))',
        r'timings?\s*:\s*(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm))',
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        parsed = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', match.group(1))
        if not parsed:
            continue
        hour = int(parsed.group(1))
        minute = int(parsed.group(2) or 0)
        if not 1 <= hour <= 12 or minute > 59:
            continue
        if parsed.group(3) == 'pm' and hour != 12:
            hour += 12
        elif parsed.group(3) == 'am' and hour == 12:
            hour = 0
        return f'{hour:02d}:{minute:02d}'
    return None


def event_description(soup):
    main = soup.select_one('main')
    if not main:
        return None
    copy = BeautifulSoup(str(main), 'html.parser')
    for element in copy.select(
        'nav, .c-anchors, .c-event-details__booking, .c-event-details__mobile-booking, '
        '.c-event-card, .c-col-gallery, button, script, style'
    ):
        element.decompose()
    description = clean_text(copy)
    return description or None


def venue_and_city(soup, default_venue, default_city):
    booking = clean_text(soup.select_one('.c-event-details__booking'))
    match = re.search(r'(?:^|\n)Venue\s*:?\s*\n([^\n]+)', booking, re.IGNORECASE)
    venue = clean_text(match.group(1)) if match else default_venue
    main_text = clean_text(soup.select_one('main'))
    if 'Royal Albert Hall' in main_text:
        return 'Royal Albert Hall', 'London'
    return venue, default_city


def parse_event(url, metadata, soup):
    title = clean_text(soup.select_one('h1.c-masthead__title'))
    date_text = (
        clean_text(soup.select_one('.c-event-details__daterange'))
        or metadata.get('listing_date', '')
    )
    dates = event_dates(date_text, metadata['year'])
    venue, city = venue_and_city(soup, metadata['venue'], metadata['city'])
    time_from = parse_time(clean_text(soup.select_one('.c-event-details__booking')))
    description = event_description(soup)
    if not title or not dates or not venue or not city:
        return []
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def get_concerts():
    session = build_session()
    events = discovery_events(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_soup, session, url): (url, metadata)
            for url, metadata in events.items()
        }
        for future in as_completed(futures):
            url, metadata = futures[future]
            try:
                records.extend(parse_event(url, metadata, future.result()))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Garsington Opera event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class GarsingtonOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='garsingtonopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    GarsingtonOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
