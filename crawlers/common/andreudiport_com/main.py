import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://andreudiport.com/'
CALENDAR_URL = f'{SOURCE_URL}calendar'
SOURCE = 'Andreu Diport'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'JANUARY': 1, 'FEBRUARY': 2, 'MARCH': 3, 'APRIL': 4,
    'MAY': 5, 'JUNE': 6, 'JULY': 7, 'AUGUST': 8,
    'SEPTEMBER': 9, 'OCTOBER': 10, 'NOVEMBER': 11, 'DECEMBER': 12,
}

COUNTRY_BY_CITY = {
    'LOS ANGELES': 'US',
    'MILAN': 'IT',
    'WARSAW': 'PL',
    'GDAŃSK': 'PL',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def parse_dates(value):
    match = re.fullmatch(
        r'(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s+'
        r'(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|'
        r'SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{4})',
        value.strip(),
        re.IGNORECASE,
    )
    if not match:
        return []
    first_day, last_day, month_name, year = match.groups()
    days = [int(first_day)]
    if last_day:
        days.extend(range(int(first_day) + 1, int(last_day) + 1))
    try:
        return [
            date(int(year), MONTHS[month_name.upper()], day).isoformat()
            for day in days
        ]
    except ValueError:
        return []


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*([AP])M\b', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'P':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_location(value):
    location = re.sub(r'\s+-\s+\d{1,2}:[0-5]\d\s*[AP]M\s*$', '', value, flags=re.IGNORECASE)
    match = re.fullmatch(r'(.+?)\s*\(([^()]*)\)', location)
    if not match:
        return None

    venue = re.sub(r'\s+', ' ', match.group(1)).strip(' ,')
    place = re.sub(r'\s+', ' ', match.group(2)).strip()
    place_upper = place.upper()

    if place_upper == 'USA':
        # Cards such as "PROVIDENCE (USA)" name a city, not a venue.
        return None
    if place_upper == 'SLOVAKIA':
        return None
    if place_upper == 'UNITED KINGDOM' or place_upper == 'LITHUANIA':
        return None

    city = place.split(',', 1)[0].strip()
    country_code = COUNTRY_BY_CITY.get(city.upper(), 'ES')
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city.title(), country_code


def event_title(description):
    if not description:
        return ''
    first_line = description.split('\n', 1)[0]
    return re.sub(r'\s*\(\d{4}(?:\s*-\s*\d{4})?\).*$', '', first_line).strip(' .')


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for card in soup.select('.sub.item-box.page-box'):
        date_node = card.select_one('h2.preview-title')
        subtitle_node = card.select_one('.preview-subtitle')
        body_node = card.select_one('.preview-body')
        event_dates = parse_dates(clean_text(date_node))
        location_text = clean_text(subtitle_node)
        location = parse_location(location_text)
        description = clean_text(body_node)
        title = event_title(description)
        if not event_dates or not location or not title:
            continue

        venue, city, country_code = location
        for event_date in event_dates:
            records.append({
                'title': title,
                'date': event_date,
                'url': CALENDAR_URL,
                'time_from': parse_time(location_text),
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class AndreudiportComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='andreudiport_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        try:
            response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Andreu Diport calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_calendar(response.text)
        log_message(
            'Parsed Andreu Diport calendar',
            event='crawler_scrape_completed',
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return records


def main():
    AndreudiportComCrawler().run()


if __name__ == '__main__':
    main()
