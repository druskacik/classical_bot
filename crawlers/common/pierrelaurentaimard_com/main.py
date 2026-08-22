import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pierrelaurentaimard.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
SOURCE = 'Pierre-Laurent Aimard'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

# The artist's calendar is international. These are the country labels used by
# the site in its current and recent touring territory.
COUNTRY_CODES = {
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'china': 'CN',
    'czech republic': 'CZ',
    'czechia': 'CZ',
    'denmark': 'DK',
    'england': 'GB',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'hong kong': 'HK',
    'hungary': 'HU',
    'ireland': 'IE',
    'italy': 'IT',
    'japan': 'JP',
    'liechtenstein': 'LI',
    'luxembourg': 'LU',
    'monaco': 'MC',
    'netherlands': 'NL',
    'new zealand': 'NZ',
    'norway': 'NO',
    'poland': 'PL',
    'portugal': 'PT',
    'scotland': 'GB',
    'singapore': 'SG',
    'south korea': 'KR',
    'spain': 'ES',
    'sweden': 'SE',
    'switzerland': 'CH',
    'taiwan': 'TW',
    'united kingdom': 'GB',
    'united states': 'US',
    'united states of america': 'US',
    'usa': 'US',
    'wales': 'GB',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    """Return every performance date represented by the calendar date label."""
    text = clean_text_value(value).lower()
    year_matches = re.findall(r'\b(20\d{2})\b', text)
    if not year_matches:
        return []
    default_year = int(year_matches[-1])

    month_pattern = '|'.join(MONTHS)
    month_matches = list(re.finditer(rf'\b({month_pattern})\b', text))
    parsed = []
    previous_end = 0
    for index, month_match in enumerate(month_matches):
        prefix = text[previous_end:month_match.start()]
        days = [int(day) for day in re.findall(r'\b([0-3]?\d)\b', prefix)]
        if not days:
            previous_end = month_match.end()
            continue

        next_month_start = (
            month_matches[index + 1].start()
            if index + 1 < len(month_matches)
            else len(text)
        )
        suffix = text[month_match.end():next_month_start]
        year_match = re.search(r'\b(20\d{2})\b', suffix)
        year = int(year_match.group(1)) if year_match else default_year
        for day in days:
            try:
                parsed.append(date(year, MONTHS[month_match.group(1)], day).isoformat())
            except ValueError:
                continue
        previous_end = month_match.end()

    return list(dict.fromkeys(parsed))


def clean_text_value(value):
    value = value.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', value).strip()


def parse_location(value):
    location = clean_text_value(value)
    parts = [part.strip() for part in re.split(r'\s*,\s*', location) if part.strip()]
    if len(parts) < 2:
        return None

    country_label = parts[-1].lower().rstrip('.')
    country_code = COUNTRY_CODES.get(country_label)
    if not country_code:
        return None

    city = ', '.join(parts[:-1]).strip()
    # This one listing includes the festival name in the location field rather
    # than just the city, while still identifying Scotland as the country.
    if city.lower() == 'edinburgh international festival':
        city = 'Edinburgh'
    if not city:
        return None
    return city, country_code


def parse_calendar_item(item):
    date_label = clean_text(item.select_one('.top-day'))
    dates = parse_dates(date_label)
    location = parse_location(clean_text(item.select_one('.city')))
    venue = clean_text(item.select_one('.upcoming-venue'))
    if not dates or not location or not venue:
        return []

    city, country_code = location
    description = clean_text(item.select_one('.upcoming-notes')) or None
    ticket_link = item.select_one('a.meta-title[href]')
    event_url = urljoin(CALENDAR_URL, ticket_link['href']) if ticket_link else CALENDAR_URL
    title = f'{SOURCE} — {venue}'

    return [
        {
            'title': title,
            'date': event_date,
            'url': event_url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class PierreLaurentAimardComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pierrelaurentaimard_com',
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
        try:
            response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Pierre-Laurent Aimard calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for item in soup.select('.calendar-items'):
            records.extend(parse_calendar_item(item))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['city'], record['venue'], record['url']
            ),
        )


def main():
    PierreLaurentAimardComCrawler().run()


if __name__ == '__main__':
    main()
