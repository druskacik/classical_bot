import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ryedalefestival.com/'
SOURCE = 'Ryedale Festival'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'jan': 1,
    'feb': 2,
    'mar': 3,
    'apr': 4,
    'may': 5,
    'jun': 6,
    'jul': 7,
    'aug': 8,
    'sep': 9,
    'oct': 10,
    'nov': 11,
    'dec': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value, year):
    match = re.search(
        r'\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None

    month = MONTHS.get(match.group(2)[:3].lower())
    if month is None:
        return None, None
    try:
        event_date = date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None

    hour = int(match.group(3))
    minute = int(match.group(4) or '00')
    if hour not in range(1, 13) or minute > 59:
        return None, None
    if match.group(5).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(5).lower() == 'am' and hour == 12:
        hour = 0
    return event_date, f'{hour:02d}:{minute:02d}'


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or '00')
    if hour not in range(1, 13) or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def infer_city(displayed_city, address):
    displayed_city = displayed_city.strip()
    venue_part = address.split(',', 1)[0].strip()
    if displayed_city and displayed_city.casefold() != venue_part.casefold():
        return displayed_city

    before_postcode = re.sub(
        r'\s+[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\s*$', '', address, flags=re.IGNORECASE
    )
    parts = [part.strip() for part in before_postcode.split(',') if part.strip()]
    if len(parts) >= 2:
        return parts[-1]
    return ''


def extract_description(widgets):
    sections = []
    in_description = False
    for widget in widgets:
        widget_id = widget.get('data-id')
        widget_type = widget.get('data-widget_type', '')
        if widget_id == 'c976014':
            in_description = True
            continue
        if widget_id == 'ecb6785':
            break
        if widget_id == '890f311':
            continue
        if in_description and widget_type in {'heading.default', 'text-editor.default'}:
            text = clean_text(widget)
            if text:
                sections.append(text)
    return '\n\n'.join(sections) or None


def parse_event(page_html, url, year):
    soup = BeautifulSoup(page_html, 'html.parser')
    event = soup.select_one('[data-elementor-type="single-post"]')
    if event is None:
        return None

    title = clean_text(event.select_one('[data-id="e846920"]'))
    date_text = clean_text(event.select_one('[data-id="7ad5e8f"]'))
    displayed_city = clean_text(event.select_one('[data-id="a1c056d"]'))
    address = clean_text(event.select_one('[data-id="890f311"]'))
    event_date, time_from = parse_date_time(date_text, year)
    venue = address.split(',', 1)[0].strip()
    city = infer_city(displayed_city, address)
    common = {
        'title': title,
        'date': event_date,
        'url': url,
        'country_code': 'GB',
        'description': extract_description(event.select('.elementor-widget')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    if all((title, event_date, url, venue, city)):
        return [{**common, 'time_from': time_from, 'venue': venue, 'city': city}]

    # Occasionally one event page contains multiple performances and deliberately
    # leaves the standard location fields empty.  Its Venue details block lists
    # each occurrence as a time followed by a full venue address.
    page_text = clean_text(event)
    venue_details = page_text.partition('Venue details:')[2].partition('\nTickets')[0]
    occurrence_pattern = re.compile(
        r'(\d{1,2}(?:[.:]\d{2})?\s*(?:am|pm))\n'
        r'([^\n]+?\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})',
        re.IGNORECASE,
    )
    records = []
    for occurrence_time, occurrence_address in occurrence_pattern.findall(venue_details):
        occurrence_venue = occurrence_address.split(',', 1)[0].strip()
        occurrence_city = infer_city(occurrence_venue, occurrence_address)
        parsed_time = parse_time(occurrence_time)
        if all((title, event_date, occurrence_venue, occurrence_city, parsed_time)):
            records.append(
                {
                    **common,
                    'time_from': parsed_time,
                    'venue': occurrence_venue,
                    'city': occurrence_city,
                }
            )
    return records


def fetch_event_page(url):
    last_error = None
    for _attempt in range(2):
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
    raise last_error


class RyedaleFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ryedalefestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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

    def _get_event_index(self, session):
        response = session.get(API_URL, params={'per_page': 100, 'page': 1}, timeout=60)
        response.raise_for_status()
        events = response.json()
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        for page in range(2, total_pages + 1):
            response = session.get(
                API_URL, params={'per_page': 100, 'page': page}, timeout=60
            )
            response.raise_for_status()
            events.extend(response.json())
        return events

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            home_response = session.get(SOURCE_URL, timeout=60)
            home_response.raise_for_status()
            year_matches = re.findall(r'\b20\d{2}\b', home_response.text)
            if not year_matches:
                raise ValueError('Could not determine the programme year')
            programme_year = max(int(value) for value in year_matches)
            events = self._get_event_index(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Ryedale Festival event index',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_event_page, item['link']): item
                for item in events
                if item.get('link')
            }
            for future in as_completed(futures):
                item = futures[future]
                url = item['link']
                try:
                    page_html = future.result()
                    records.extend(parse_event(page_html, url, programme_year))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Ryedale Festival event',
                        event='crawler_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    RyedaleFestivalComCrawler().run()


if __name__ == '__main__':
    main()
