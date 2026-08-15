import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://augustasymphony.com/'
SOURCE = 'Augusta Symphony'
LISTING_URL = f'{SOURCE_URL}events/?rhp_bar_rhp_gen=Concerts'
CITY = 'Augusta'
VENUE = 'Miller Theater'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+)?([A-Za-z]+)\s+(\d{1,2})', re.I)
TIME_RE = re.compile(r'\bShow:\s*(\d{1,2}(?::\d{2})?\s*[ap]m)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)).strip()


def parse_upcoming_date(value, today=None):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None

    today = today or date.today()
    try:
        parsed = datetime.strptime(f'{match.group(1)} {match.group(2)} {today.year}', '%B %d %Y').date()
    except ValueError:
        try:
            parsed = datetime.strptime(f'{match.group(1)} {match.group(2)} {today.year}', '%b %d %Y').date()
        except ValueError:
            return None

    # The first-party feed is an upcoming season calendar and omits the year.
    # Dates well behind today therefore belong to the following calendar year.
    if (today - parsed).days > 31:
        try:
            parsed = parsed.replace(year=parsed.year + 1)
        except ValueError:
            return None
    return parsed.isoformat()


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(match.group(1).upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_items(soup, today=None):
    items = []
    seen = set()
    for card in soup.select('.rhpSingleEvent'):
        title_node = card.select_one('.eventTitleDiv h2, #eventTitle h2')
        link = card.select_one('.eventTitleDiv a[href], a.url[href*="/event/"]')
        date_node = card.select_one('.singleEventDate, #eventDate')
        title = clean_text(title_node)
        url = urljoin(LISTING_URL, link.get('href', '')) if link else ''
        event_date = parse_upcoming_date(date_node, today=today)
        if not title or not event_date or not url.startswith(SOURCE_URL + 'event/'):
            continue
        # Package pages have a price and date but represent a season series,
        # rather than a concrete performance occurrence.
        if re.search(r'\b\d{4}-\d{2}\b.*\bseries\b', title, re.I):
            continue
        key = (url, event_date)
        if key not in seen:
            seen.add(key)
            items.append((title, event_date, url))
    return items


def detail_record(session, title, event_date, url):
    soup = get_soup(session, url)
    description_node = soup.select_one('.singleEventDescription')
    time_node = soup.select_one('.eventDoorStartDate')
    ticket_links = [link.get('href', '') for link in soup.select('.rhp-event-cta a[href]')]

    # The plugin does not render its assigned venue, but every selected
    # performance's first-party ticket link names Augusta Symphony at Miller
    # Theater. Do not apply that home-venue default to a touring event.
    if not any('augusta-symphony-at-miller-theater' in link.lower() for link in ticket_links):
        log_message(
            'Skipping event without Miller Theater evidence',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            error_type='MissingVenueEvidence',
        )
        return None

    description = clean_text(description_node) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_node),
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None, today=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, LISTING_URL)
    items = listing_items(soup, today=today)
    records = []
    for item in items:
        try:
            record = detail_record(session, *item)
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=item[2],
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class AugustaSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='augustasymphony_com',
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
    AugustaSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
