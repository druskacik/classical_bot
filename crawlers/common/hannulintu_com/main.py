import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hannulintu.com/'
SOURCE = 'Hannu Lintu'
CALENDAR_PATHS = ('calendar', 'calendar-past-events')
PAGINATION_PARAMETER = '37662aed_page'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'china': 'CN',
    'czech republic': 'CZ',
    'denmark': 'DK',
    'estonia': 'EE',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'hong kong': 'HK',
    'hungary': 'HU',
    'iceland': 'IS',
    'ireland': 'IE',
    'italy': 'IT',
    'japan': 'JP',
    'latvia': 'LV',
    'lithuania': 'LT',
    'luxembourg': 'LU',
    'netherlands': 'NL',
    'norway': 'NO',
    'poland': 'PL',
    'portugal': 'PT',
    'singapore': 'SG',
    'south korea': 'KR',
    'spain': 'ES',
    'sweden': 'SE',
    'switzerland': 'CH',
    'taiwan': 'TW',
    'united kingdom': 'GB',
    'uk': 'GB',
    'united states': 'US',
    'united states of america': 'US',
    'usa': 'US',
}

VENUE_WORDS = re.compile(
    r'\b(?:auditorium|cathedral|center|centre|church|concertgebouw|hall|opera|'
    r'palace|philharmonie|theater|theatre|tonhalle)\b|musiikkitalo|gulbenkian',
    re.IGNORECASE,
)

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_location(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return None

    country_index = next(
        (index for index in range(len(parts) - 1, 0, -1)
         if parts[index].casefold() in COUNTRY_CODES),
        None,
    )
    if country_index is None:
        return None
    country_name = parts[country_index].casefold()
    if country_name in {'singapore', 'hong kong'}:
        city = parts[country_index]
    else:
        city = parts[country_index - 1]
    if not city:
        return None
    return city, COUNTRY_CODES[country_name]


def parse_venue(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    # The first component is the performing orchestra. Later components are
    # festival/venue data; prefer an explicitly venue-like component.
    candidates = parts[1:]
    return next((part for part in reversed(candidates) if VENUE_WORDS.search(part)), None)


def parse_item(item, page_url):
    year_text = clean_text(item.select_one('.date-year'))
    month_text = clean_text(item.select_one('.date-month')).casefold()[:3]
    day_text = clean_text(item.select_one('.date-day'))
    try:
        event_date = date(int(year_text), MONTHS[month_text], int(day_text)).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    heading = clean_text(item.select_one('[fs-list-field="city, country"]'))
    location = parse_location(clean_text(item.select_one('[fs-list-field="location"]')))
    venue = parse_venue(heading)
    if not heading or not location or not venue:
        return None

    description_parts = [
        clean_text(item.select_one('[fs-list-field="info-left"]')),
        clean_text(item.select_one('[fs-list-field="info-right"]')),
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None
    city, country_code = location

    return {
        'title': heading,
        'date': event_date,
        'url': page_url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HannuLintuComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hannulintu_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []

        for calendar_path in CALENDAR_PATHS:
            page_number = 1
            while page_number <= 100:
                page_url = urljoin(SOURCE_URL, calendar_path)
                params = {PAGINATION_PARAMETER: page_number} if page_number > 1 else None
                try:
                    response = session.get(page_url, params=params, timeout=45)
                    response.raise_for_status()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Hannu Lintu calendar page',
                        event='crawler_fetch_failed',
                        level='error',
                        url=page_url,
                        page_number=page_number,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    raise

                soup = BeautifulSoup(response.text, 'html.parser')
                items = soup.select('.event31_accordion')
                for accordion in items:
                    item = accordion.find_parent(attrs={'role': 'listitem'})
                    if item is None:
                        continue
                    record = parse_item(item, response.url)
                    if record:
                        records.append(record)

                next_link = soup.select_one('.w-pagination-next[href]')
                if next_link is None:
                    break
                page_number += 1

        return sorted(
            records,
            key=lambda record: (record['date'], record['title'], record['city']),
        )


def main():
    HannuLintuComCrawler().run()


if __name__ == '__main__':
    main()
