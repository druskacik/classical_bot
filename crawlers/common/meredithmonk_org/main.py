import calendar
import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://meredithmonk.org/'
SOURCE = 'Meredith Monk'
API_URL = (
    'https://wordpress-dot-meredith-monk-website.appspot.com/'
    'wp-json/wp/v2/pages'
)
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/current')

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(calendar.month_name)
    if name
}
MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})

COUNTRIES = {
    'Australia': 'AU',
    'Austria': 'AT',
    'Brazil': 'BR',
    'Canada': 'CA',
    'Denmark': 'DK',
    'England': 'GB',
    'France': 'FR',
    'Germany': 'DE',
    'Hungary': 'HU',
    'Ireland': 'IE',
    'Italy': 'IT',
    'Latvia': 'LV',
    'Lithuania': 'LT',
    'Luxembourg': 'LU',
    'Norway': 'NO',
    'Poland': 'PL',
    'Portugal': 'PT',
    'Russia': 'RU',
    'Scotland': 'GB',
    'Spain': 'ES',
    'The Netherlands': 'NL',
    'Netherlands': 'NL',
    'Ukraine': 'UA',
    'United Arab Emirates': 'AE',
    'Wales': 'GB',
}

US_REGIONS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DC', 'DE', 'FL', 'GA', 'HI',
    'IA', 'ID', 'IL', 'IN', 'KS', 'KY', 'LA', 'MA', 'MD', 'ME', 'MI', 'MN',
    'MO', 'MS', 'MT', 'NC', 'ND', 'NE', 'NH', 'NJ', 'NM', 'NV', 'NY', 'OH',
    'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VA', 'VT', 'WA',
    'WI', 'WV', 'WY',
}
US_REGION_NAMES = {
    'California', 'Colorado', 'Connecticut', 'Florida', 'Illinois', 'Maryland',
    'Massachusetts', 'Michigan', 'Minnesota', 'Missouri', 'Montana',
    'New Mexico', 'New York', 'North Carolina', 'Rhode Island', 'Tennessee',
    'Texas', 'Vermont', 'Washington',
}
CANADIAN_REGIONS = {'QC'}


def clean_text(value):
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def parse_time(value):
    match = re.search(r'\b(1[0-2]|0?[1-9]):([0-5]\d)\s*([AP]M)\b', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'PM':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_event_dates(value, sort_date):
    """Expand short, explicit runs; retain the API date for less regular notation."""
    value = clean_text(value).replace('\u2013', '-').replace('\u2014', '-')
    try:
        fallback = date.fromisoformat(clean_text(sort_date))
    except ValueError:
        return []

    month_match = re.search(
        r'\b(' + '|'.join(re.escape(month) for month in MONTHS) + r')\.?\s+' r'(.+?)\s*,\s*(20\d{2})\b',
        value,
        re.I,
    )
    if not month_match:
        return [fallback.isoformat()]

    month = MONTHS[month_match.group(1).lower().rstrip('.')]
    year = int(month_match.group(3))
    day_text = month_match.group(2)
    day_numbers = [int(item) for item in re.findall(r'\b([0-3]?\d)(?:st|nd|rd|th)?\b', day_text, re.I)]
    day_numbers = [day for day in day_numbers if 1 <= day <= 31]
    if not day_numbers:
        return [fallback.isoformat()]

    if re.fullmatch(r'\s*\d{1,2}(?:st|nd|rd|th)?\s*-\s*\d{1,2}(?:st|nd|rd|th)?\s*', day_text, re.I):
        start_day, end_day = day_numbers[:2]
        if end_day >= start_day and end_day - start_day <= 6:
            try:
                start = date(year, month, start_day)
                end = date(year, month, end_day)
                return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
            except ValueError:
                return [fallback.isoformat()]

    if '&' in day_text or re.search(r',\s*\d', day_text):
        parsed = []
        for day in day_numbers:
            try:
                parsed.append(date(year, month, day).isoformat())
            except ValueError:
                continue
        return list(dict.fromkeys(parsed)) or [fallback.isoformat()]

    try:
        return [date(year, month, day_numbers[0]).isoformat()]
    except ValueError:
        return [fallback.isoformat()]


def parse_location(value):
    location = clean_text(value).strip(' ,()')
    if not location or re.search(r'\b(?:online|zoom|streamed|broadcast|youtube|facebook)\b', location, re.I):
        return None

    normalized = re.sub(r'\s*[()]\s*', ', ', location)
    parts = [part.strip(' ,') for part in normalized.split(',') if part.strip(' ,')]
    if len(parts) < 2:
        return None

    last = parts[-1]
    if last in US_REGIONS or last in US_REGION_NAMES:
        country_code = 'US'
        city = parts[-2]
    elif last in CANADIAN_REGIONS:
        country_code = 'CA'
        city = parts[-2]
    elif last in COUNTRIES:
        country_code = COUNTRIES[last]
        city = parts[-2]
    else:
        return None

    city = clean_text(city)
    city = {'NYC': 'New York', 'NY': 'New York'}.get(city, city)
    if (
        not city
        or city == location
        or re.search(r'\b(?:center|centre|hall|museum|theat(?:er|re)|university|college)\b', city, re.I)
    ):
        return None

    # A two-part location normally contains only a city and country, not a venue.
    if len(parts) == 2 and parts[0].casefold() == city.casefold():
        return None

    return location, city, country_code


def event_url(event, page_slug):
    link = event.get('link')
    if isinstance(link, dict):
        url = clean_text(link.get('url'))
        if url:
            return url
    return urljoin(SOURCE_URL, f'calendar/{page_slug}')


def records_from_pages(pages):
    records = []
    for page in pages:
        slug = clean_text(page.get('slug')) or 'current'
        events = (page.get('acf') or {}).get('events') or []
        for event in events:
            title = clean_text(event.get('name'))
            location = parse_location(event.get('location'))
            dates = parse_event_dates(event.get('date'), event.get('sort_date'))
            if not title or not location or not dates:
                continue
            venue, city, country_code = location
            for event_date in dates:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': event_url(event, slug),
                    'time_from': parse_time(clean_text(event.get('date'))),
                    'venue': venue,
                    'city': city,
                    'country_code': country_code,
                    'description': None,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class MeredithmonkOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='meredithmonk_org',
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
            response = requests.get(
                API_URL,
                params={'parent': 1099, 'per_page': 100},
                headers=HEADERS,
                timeout=60,
            )
            response.raise_for_status()
            pages = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Meredith Monk calendar',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not isinstance(pages, list):
            raise ValueError('Meredith Monk calendar API returned an unexpected response')

        records = records_from_pages(pages)
        if not records:
            log_message(
                'No parseable Meredith Monk calendar events found',
                event='crawler_empty_listing',
                level='warning',
                url=CALENDAR_URL,
                record_count=0,
            )
        return records


def main():
    MeredithmonkOrgCrawler().run()


if __name__ == '__main__':
    main()
