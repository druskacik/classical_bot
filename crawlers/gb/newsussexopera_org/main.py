import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.newsussexopera.org/'
SOURCE = 'New Sussex Opera'
BOOKING_PAGE_ID = 8561
BOOKING_API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages/{BOOKING_PAGE_ID}'
COUNTRY_CODE = 'GB'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}
WEEKDAYS = {
    name.lower(): number
    for number, name in enumerate(
        ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
    )
}

PERFORMANCE_RE = re.compile(
    r'^\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(\d{1,2})(?:st|nd|rd|th)?\s+at\s+'
    r'(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\s+(.+?)\s*$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True)
        if '<' in raw or '&' in raw
        else raw
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_booking_page(session):
    response = session.get(BOOKING_API_URL, timeout=45)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get('content', {}).get('rendered'):
        raise ValueError('Unexpected booking page response')
    return payload


def resolve_year(weekday, month, day, modified):
    expected_weekday = WEEKDAYS[weekday.lower()]
    candidates = []
    for year in range(modified.year - 1, modified.year + 2):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate.weekday() == expected_weekday:
            candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs((candidate - modified.date()).days))


def parse_time(hour_text, minute_text, meridiem):
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if hour not in range(1, 13) or minute not in range(60):
        return None
    if meridiem.lower() == 'pm' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_location(value):
    value = re.sub(r'\s+(?:click here|book(?:ing)?(?: now)?)\b.*$', '', value, flags=re.I)
    value = re.sub(r'\s+[–-]\s*$', '', value).strip(' ,–-')
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return None, None
    city = re.sub(r'\s+[A-Z]{1,2}\d[A-Z\d]?\b.*$', '', parts[-1], flags=re.I).strip()
    venue = ', '.join(parts[:-1]).strip()
    if not venue or not city:
        return None, None
    return venue.title(), city.title()


def parse_booking_page(payload):
    soup = BeautifulSoup(payload['content']['rendered'], 'html.parser')
    heading = clean_text(soup.select_one('h1, h2'))
    title = re.sub(r'^Booking Details for\s+', '', heading, flags=re.I).strip()
    try:
        modified = datetime.fromisoformat(payload['modified'])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Booking page has no usable modification date') from error

    booking_url = clean_text(payload.get('link')) or f'{SOURCE_URL}booking-details/'
    description_parts = [title] if title else []
    records = []
    for element in soup.select('h3'):
        text = clean_text(element)
        match = PERFORMANCE_RE.match(text)
        if not match:
            continue
        weekday, month_name, day_text, hour, minute, meridiem, location_text = match.groups()
        event_date = resolve_year(
            weekday.title(), MONTHS[month_name.lower()], int(day_text), modified
        )
        time_from = parse_time(hour, minute, meridiem)
        venue, city = parse_location(location_text)
        if not title or not event_date or not time_from or not venue or not city:
            continue
        description_parts.append(text)
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': booking_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    description = '\n'.join(dict.fromkeys(description_parts)) or None
    for record in records:
        record['description'] = description
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        payload = get_booking_page(session)
        records = parse_booking_page(payload)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to scrape New Sussex Opera booking page',
            event='crawler_failed',
            level='error',
            url=BOOKING_API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class NewSussexOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='newsussexopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    NewSussexOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
