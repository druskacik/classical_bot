import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://desmoinesmetroopera.org/'
SOURCE = 'Des Moines Metro Opera'
CALENDAR_URL = urljoin(SOURCE_URL, 'events/')
MONTHS = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# Some calendar entries abbreviate their location on the monthly page and do
# not publish a postal address on the detail page.
KNOWN_CITIES = {
    'Blank Performing Arts Center': 'Indianola',
    'Lekberg Hall': 'Indianola',
    'Simpson College': 'Indianola',
    'Sheslow Auditorium': 'Des Moines',
    'Drake University': 'Des Moines',
    'Krause Gateway Center': 'Des Moines',
    'Franklin Avenue Library': 'Des Moines',
    'Wakonda Club': 'Des Moines',
    'Hoyt Sherman Place': 'Des Moines',
    'Des Moines Civic Center': 'Des Moines',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True) if hasattr(element, 'get_text') else str(element)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def shift_month(year, month, offset):
    value = year * 12 + month - 1 + offset
    return divmod(value, 12)[0], divmod(value, 12)[1] + 1


def parse_date_and_time(value):
    match = re.search(
        r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})'
        r'(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?',
        value,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        event_time = (
            datetime.strptime(re.sub(r'\s+', '', match.group(2)), '%I:%M%p').strftime('%H:%M')
            if match.group(2) else None
        )
        return event_date, event_time
    except ValueError:
        return None, None


def city_from_location(location):
    match = re.search(
        r',\s*([^,]+),\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\s*$', location
    )
    if match:
        return match.group(1).strip()
    for marker, city in KNOWN_CITIES.items():
        if marker.lower() in location.lower():
            return city
    return None


def venue_from_location(location):
    parts = [part.strip() for part in location.split(',') if part.strip()]
    venue_parts = []
    for part in parts:
        if re.match(r'^\d+\s', part) or re.fullmatch(
            r'[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?', part
        ):
            break
        venue_parts.append(part)
    if len(venue_parts) == len(parts) and len(parts) >= 3 and re.fullmatch(
        r'[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?', parts[-1]
    ):
        venue_parts = parts[:-2]
    return ', '.join(venue_parts).strip()


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1'))
    date_text = clean_text(soup.select_one('.event_details .date'))
    location = clean_text(soup.select_one('.event_details .address_location'))
    event_date, time_from = parse_date_and_time(date_text)
    city = city_from_location(location)
    venue = venue_from_location(location)
    description = clean_text(soup.select_one('.event_desc')) or None
    if not all((title, event_date, url, venue, city)):
        return None
    return {
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


class DesMoinesMetroOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='desmoinesmetroopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        today = date.today()
        event_urls = set()

        # Walk backward until two entire years contain no calendar entries. This
        # retains all archives exposed by the calendar without assuming its
        # launch year. Also look two years ahead for announced performances.
        month_offsets = list(range(24, 0, -1))
        empty_run = 0
        offset = 0
        while empty_run < 24 and offset > -240:
            month_offsets.append(offset)
            year, month = shift_month(today.year, today.month, offset)
            url = f'{CALENDAR_URL}{year}/{MONTHS[month - 1]}'
            try:
                response = session.get(url, timeout=30)
                response.raise_for_status()
                response.encoding = 'utf-8'
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch DMMO calendar month',
                    event='crawler_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                empty_run = 0
                offset -= 1
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
            links = {
                urljoin(SOURCE_URL, link['href'])
                for link in soup.select('.js-cal_list_view .event_item a[href*="/events/event-"]')
            }
            event_urls.update(links)
            empty_run = 0 if links else empty_run + 1
            offset -= 1

        for future_offset in month_offsets[:24]:
            year, month = shift_month(today.year, today.month, future_offset)
            url = f'{CALENDAR_URL}{year}/{MONTHS[month - 1]}'
            try:
                response = session.get(url, timeout=30)
                response.raise_for_status()
                response.encoding = 'utf-8'
                soup = BeautifulSoup(response.text, 'html.parser')
                event_urls.update(
                    urljoin(SOURCE_URL, link['href'])
                    for link in soup.select('.js-cal_list_view .event_item a[href*="/events/event-"]')
                )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch DMMO future calendar month',
                    event='crawler_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        records = []
        for url in sorted(event_urls):
            try:
                response = session.get(url, timeout=30)
                response.raise_for_status()
                response.encoding = 'utf-8'
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch DMMO event',
                    event='crawler_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            record = parse_event_page(response.text, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped DMMO event with incomplete required fields',
                    event='crawler_record_skipped', level='warning', url=url,
                )
        return records


def main():
    return DesMoinesMetroOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
