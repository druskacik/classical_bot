import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.simonlepper.com/'
CALENDAR_URL = 'https://www.simonlepper.com/schedule'
SOURCE = 'Simon Lepper'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    month.lower(): number
    for number, month in enumerate(
        [
            '', 'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ]
    )
    if month
}

COUNTRIES = {
    'france': 'FR',
    'germany': 'DE',
    'iceland': 'IS',
    'lithuania': 'LT',
    'northern ireland': 'GB',
    'uk': 'GB',
    'united kingdom': 'GB',
    'usa': 'US',
}

DATE_LINE = re.compile(
    r'^(?P<month>' + '|'.join(MONTHS) + r')\s+'
    r'(?P<days>\d{1,2}(?:st|nd|rd|th)?(?:\s*(?:-|,)\s*\d{1,2})*)$',
    re.IGNORECASE,
)


def clean_lines(element):
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    raw_lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip()]
    lines = []
    for line in raw_lines:
        if lines and lines[-1].endswith(',') and line.lower() in COUNTRIES:
            lines[-1] = f'{lines[-1]} {line}'
        else:
            lines.append(line)
    return lines


def expand_dates(value, year):
    match = DATE_LINE.fullmatch(value)
    if not match:
        return []

    month = MONTHS[match.group('month').lower()]
    day_text = re.sub(r'(st|nd|rd|th)', '', match.group('days'), flags=re.IGNORECASE)
    if '-' in day_text:
        start, end = (int(part.strip()) for part in day_text.split('-', 1))
        days = range(start, end + 1)
    else:
        days = [int(part.strip()) for part in day_text.split(',')]

    parsed = []
    for day in days:
        try:
            parsed.append(date(year, month, day).isoformat())
        except ValueError:
            continue
    return parsed


def parse_location(value):
    if ',' not in value:
        return None
    city, country = (part.strip() for part in value.rsplit(',', 1))
    country_code = COUNTRIES.get(country.lower())
    if not city or not country_code:
        return None
    return city, country_code


def parse_calendar(soup, year):
    main = soup.select_one('main')
    if main is None:
        return []

    lines = clean_lines(main)
    starts = [index for index, line in enumerate(lines) if DATE_LINE.fullmatch(line)]
    records = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = lines[start:end]
        # Short fragments left in the Wix editor are not event data.
        details = [line for line in block[1:] if line not in {'Reci', 'Jul', 'Aug', 'r'}]
        if len(details) < 3:
            continue

        location_index = next(
            (index for index in range(len(details) - 1, 0, -1) if parse_location(details[index])),
            None,
        )
        if location_index is None or location_index < 2:
            continue

        title = details[0]
        if re.search(r'\b(masterclasses?|recording|recorded)\b', title, re.IGNORECASE):
            continue

        venue = details[location_index - 1]
        city, country_code = parse_location(details[location_index])
        if not title or not venue or venue.lower() == city.lower():
            continue

        description = '\n'.join(details[:location_index + 1])
        for event_date in expand_dates(block[0], year):
            records.append({
                'title': title,
                'date': event_date,
                'url': CALENDAR_URL,
                'time_from': None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class SimonlepperComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='simonlepper_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Simon Lepper calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        return sorted(
            parse_calendar(soup, date.today().year),
            key=lambda record: (record['date'], record['title'], record['venue']),
        )


def main():
    SimonlepperComCrawler().run()


if __name__ == '__main__':
    main()
