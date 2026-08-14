import calendar
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filharmoniazg.pl/'
EVENTS_URL = urljoin(SOURCE_URL, 'index.php/events/')
SOURCE = 'Filharmonia Zielonogórska im. Tadeusza Bairda'
DEFAULT_CITY = 'Zielona Góra'
DEFAULT_VENUE = 'Filharmonia Zielonogórska im. Tadeusza Bairda'

# The current Events Manager archive begins here; earlier calendar months are
# still reachable but contain no event posts.
ARCHIVE_START = (2025, 9)
FUTURE_MONTHS = 24
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_offset(year, month, offset):
    value = year * 12 + month - 1 + offset
    return value // 12, value % 12 + 1


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def parse_month_page(html, year, month):
    soup = BeautifulSoup(html, 'html.parser')
    events = []
    for card in soup.select('main .grid-item'):
        link = card.select_one('a[href*="/events/"] h3.title')
        link = link.find_parent('a', href=True) if link else None
        date_tag = card.select_one('.dateContainer .date')
        if not link or not date_tag:
            continue
        match = re.fullmatch(r'\s*(\d{1,2})/(\d{1,2})\s*', date_tag.get_text())
        if not match or int(match.group(2)) != month:
            continue
        try:
            event_date = date(year, month, int(match.group(1))).isoformat()
        except ValueError:
            continue
        title = clean_text(link)
        if title:
            events.append({
                'title': title,
                'date': event_date,
                'url': urljoin(SOURCE_URL, link['href']),
            })
    return events


def normalize_city(value):
    city = clean_text(value).strip(' ,')
    if city.casefold() == 'zielona góra':
        return DEFAULT_CITY
    return city


def parse_detail_page(html, event):
    soup = BeautifulSoup(html, 'html.parser')
    container = soup.select_one('.em-event-single')
    if not container:
        return None

    date_text = clean_text(container.select_one('.em-event-date'))
    date_match = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', date_text)
    if date_match:
        try:
            event['date'] = date.fromisoformat(date_match.group(0)).isoformat()
        except ValueError:
            return None

    time_text = clean_text(container.select_one('.em-event-time'))
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', time_text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None

    location = container.select_one('.em-event-location .em-item-meta-line > div')
    venue_link = location.select_one('a[href*="/locations/"]') if location else None
    venue = clean_text(venue_link)
    location_lines = [line for line in clean_text(location).split('\n') if line] if location else []
    address = location_lines[-1] if location_lines else ''
    address_parts = [part.strip() for part in address.split(',') if part.strip()]
    city = normalize_city(address_parts[-1]) if address_parts else ''
    if city.casefold() == 'poland' and len(address_parts) > 1:
        city = normalize_city(address_parts[-2])

    # Unlabelled events in this venue-specific calendar take place in the
    # Philharmonic's building; touring entries have explicit location data.
    if not venue and not location:
        venue = DEFAULT_VENUE
        city = DEFAULT_CITY
    if not city and venue and 'filharmoni' in venue.casefold():
        city = DEFAULT_CITY
    if not venue or not city:
        return None

    description = clean_text(container.select_one('.em-event-content')) or None
    return {
        **event,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'PL',
        'description': description,
    }


def get_concerts():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.headers.update(HEADERS)
    # The server currently omits an intermediate certificate, while browsers
    # can repair the chain. Disabling verification is required for requests.
    session.verify = False
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))

    today = date.today()
    end_year, end_month = month_offset(today.year, today.month, FUTURE_MONTHS)
    start_index = ARCHIVE_START[0] * 12 + ARCHIVE_START[1] - 1
    end_index = end_year * 12 + end_month - 1
    months = [(value // 12, value % 12 + 1) for value in range(start_index, end_index + 1)]

    events = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {}
        for year, month in months:
            url = f'{EVENTS_URL}?month={year}-{month:02d}'
            futures[executor.submit(get_page, session, url)] = (year, month, url)
        for future in as_completed(futures):
            year, month, url = futures[future]
            try:
                events.extend(parse_month_page(future.result(), year, month))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month', event='crawler_page_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error),
                )

    events = list({(row['url'], row['date']): row for row in events}.values())
    records = []

    def load_detail(event):
        try:
            return parse_detail_page(get_page(session, event['url']), event)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape event detail', event='crawler_page_failed', level='warning',
                url=event['url'], error_type=type(error).__name__, error_message=str(error),
            )
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(load_detail, event) for event in events]
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)

    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class FilharmoniaZgPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmoniazg_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    FilharmoniaZgPlCrawler().run()


if __name__ == '__main__':
    main()
