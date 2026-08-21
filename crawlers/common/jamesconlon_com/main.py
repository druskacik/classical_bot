import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://jamesconlon.com/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'schedule/')
SOURCE = 'James Conlon'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name.lower(): number for number, name in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    ) if name
}

COUNTRIES = {
    'argentina': 'AR',
    'austria': 'AT',
    'canada': 'CA',
    'france': 'FR',
    'germany': 'DE',
    'italy': 'IT',
    'japan': 'JP',
    'netherlands': 'NL',
    'spain': 'ES',
    'switzerland': 'CH',
    'uk': 'GB',
    'united kingdom': 'GB',
    'united states': 'US',
    'usa': 'US',
}

US_STATES = {
    'CA', 'CO', 'FL', 'IL', 'MD', 'MI', 'NY', 'OH', 'TX', 'UT',
}

US_STATE_NAMES = {
    'california', 'colorado', 'florida', 'illinois', 'maryland',
    'michigan', 'new york', 'ohio', 'texas', 'utah',
}


def clean_text(element):
    if element is None:
        return ''
    return re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()


def parse_dates(value):
    """Return explicit dates; for a displayed run, retain its concrete start date."""
    text = re.sub(r'[–—]', '-', value).replace('.', ' ')
    years = [int(item) for item in re.findall(r'\b(20\d{2})\b', text)]
    if not years:
        return []

    default_year = years[-1]
    month_matches = list(re.finditer(
        r'\b(' + '|'.join(MONTHS) + r')\b', text, flags=re.IGNORECASE
    ))
    parsed = []
    for index, match in enumerate(month_matches):
        if index:
            separator = text[month_matches[index - 1].end():match.start()]
            if '-' in separator:
                # This month is the end of a displayed production run.
                continue
        month = MONTHS[match.group(1).lower()]
        end = month_matches[index + 1].start() if index + 1 < len(month_matches) else len(text)
        segment = text[match.end():end]
        segment_years = [int(item) for item in re.findall(r'\b(20\d{2})\b', segment)]
        year = segment_years[-1] if segment_years else default_year

        # A hyphen denotes a production run rather than a claim that it performs daily.
        day_part = re.split(r'\b20\d{2}\b', segment, maxsplit=1)[0]
        if '-' in day_part:
            day_part = day_part.split('-', 1)[0]
        days = [int(item) for item in re.findall(r'\b([0-3]?\d)(?:st|nd|rd|th)?\b', day_part)]
        for day in days:
            try:
                parsed.append(date(year, month, day).isoformat())
            except ValueError:
                continue

    return list(dict.fromkeys(parsed))


def parse_location(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return None

    city = parts[0]
    if '|' in city or re.search(r'\band\b', city, flags=re.IGNORECASE):
        return None

    tail = parts[-1].lower()
    country_code = COUNTRIES.get(tail)
    if country_code is None and (
        parts[-1].upper() in US_STATES or tail in US_STATE_NAMES
    ):
        country_code = 'US'
    if country_code is None:
        return None
    return city.title(), country_code


def parse_item(item, page_url):
    title = clean_text(item.select_one('.schedule-item-title'))
    venue = clean_text(item.select_one('.schedule-item-venue'))
    location = parse_location(clean_text(item.select_one('.schedule-item-location')))
    event_dates = parse_dates(clean_text(item.select_one('.schedule-item-dates')))
    if not title or not venue or not location or not event_dates:
        return []

    link = item if item.name == 'a' and item.get('href') else item.select_one('a[href]')
    show_id = item.get('data-show-id')
    if show_id is None:
        container = item.find_parent(class_='schedule-item')
        show_id = container.get('data-show-id') if container else None
    url = urljoin(page_url, link['href']) if link else f'{page_url}#show-{show_id}'
    description = clean_text(item.select_one('.schedule-item-description')) or None
    city, country_code = location

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
        }
        for event_date in event_dates
    ]


class JamesconlonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jamesconlon_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for page_url in (SCHEDULE_URL, f'{SCHEDULE_URL}?type=past'):
            try:
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch James Conlon schedule',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.text, 'html.parser')
            for item in soup.select('.schedule-item'):
                records.extend(parse_item(item, page_url))

        unique = {
            (record['title'], record['date'], record['venue'], record['city']): record
            for record in records
        }
        return sorted(
            unique.values(),
            key=lambda record: (record['date'], record['title'], record['venue']),
        )


def main():
    JamesconlonComCrawler().run()


if __name__ == '__main__':
    main()
