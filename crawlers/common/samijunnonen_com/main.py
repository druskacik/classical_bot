import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.samijunnonen.com/'
SOURCE = 'Sami Junnonen'
ARCHIVE_URLS = (
    'https://www.samijunnonen.com/events',
    'https://www.samijunnonen.com/past-events',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        1,
    )
}

COUNTRY_CODES = {
    'Austria': 'AT',
    'Belgium': 'BE',
    'Bulgaria': 'BG',
    'China': 'CN',
    'Colombia': 'CO',
    'Egypt': 'EG',
    'Estonia': 'EE',
    'Finland': 'FI',
    'France': 'FR',
    'French Caribbean': 'GP',
    'Germany': 'DE',
    'Guadeloupe': 'GP',
    'Hungary': 'HU',
    'Italy': 'IT',
    'Japan': 'JP',
    'Kosovo': 'XK',
    'Latvia': 'LV',
    'Lithuania': 'LT',
    'Mexico': 'MX',
    'Netherlands': 'NL',
    'Norway': 'NO',
    'Poland': 'PL',
    'Portugal': 'PT',
    'Russia': 'RU',
    'Slovakia': 'SK',
    'Spain': 'ES',
    'Sweden': 'SE',
    'Switzerland': 'CH',
    'UK': 'GB',
    'United Kingdom': 'GB',
    'USA': 'US',
    'United States': 'US',
}

MONTH_PATTERN = '|'.join(MONTHS)
DATE_RE = re.compile(
    rf'^({MONTH_PATTERN})\s+(\d{{1,2}}),\s*(20\d{{2}})'
    r'(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm))?$',
    re.I,
)
PARTIAL_DATE_RE = re.compile(
    rf'^({MONTH_PATTERN})\s+(\d{{1,2}})'
    r'(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm))?$',
    re.I,
)
TIME_SUFFIX_RE = re.compile(r'\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', re.I)
LOCATION_RE = re.compile(r'^(.+?),\s*([^,]+)$')
ADDRESS_RE = re.compile(
    r'(?:\b(?:FI-|A-)\d{4,6}\b|\b\d{5}\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|'
    r'\b(?:street|road|avenue|boulevard|square|st\.?|rd\.?|ave\.?|katu|tie)\b)',
    re.I,
)


def clean_lines(element):
    lines = []
    for paragraph in element.select('p, h1, h2, h3, h4, h5, h6'):
        value = paragraph.get_text(' ', strip=True)
        value = value.replace('\xa0', ' ').replace('\u200b', '').strip()
        value = re.sub(r'\s+', ' ', value)
        if value and (not lines or value != lines[-1]):
            lines.append(value)
    return lines


def parse_time(hour, minute, meridiem):
    if not hour:
        return None
    hour = int(hour)
    if meridiem.lower() == 'pm' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{int(minute or 0):02d}'


def parse_location(line):
    match = LOCATION_RE.match(line)
    if not match:
        return None
    country_name = match.group(2).strip()
    country_code = COUNTRY_CODES.get(country_name)
    if not country_code:
        return None
    city = match.group(1).strip()
    if ' & ' in city:
        return None
    return city, country_code


def sensible_venue(value):
    value = value.strip(' ()')
    lowered = value.casefold()
    rejected = (
        'admission', 'ticket', 'featuring', 'sami junnonen', 'for more',
        'program', 'programme', 'address', 'online', 'listen', 'concert series',
    )
    if not value or any(lowered.startswith(item) for item in rejected):
        return None
    if re.match(r'^(?:FI-|A-)?\d{4,5}\b', value):
        return None
    return value


def listing_title(lines, location_index):
    candidates = lines[:location_index]
    candidates = [line for line in candidates if not line.lower().startswith('image:')]
    return ' – '.join(candidates[:3]).strip()


def parse_listing(element, page_url):
    lines = clean_lines(element)
    if not lines:
        return []
    description = '\n'.join(lines)

    location_index = None
    default_location = None
    for index, line in enumerate(lines):
        location = parse_location(line)
        if location:
            location_index = index
            default_location = location
            break
    if default_location is None:
        return []

    title = listing_title(lines, location_index)
    if not title or 'masterclass' in title.casefold():
        return []

    explicit_dates = []
    inherited_year = None
    for index, line in enumerate(lines):
        range_year = re.search(rf'^(?:{MONTH_PATTERN}).*\b(20\d{{2}})\b$', line, re.I)
        if range_year:
            inherited_year = int(range_year.group(1))
        match = DATE_RE.match(line)
        if match:
            inherited_year = int(match.group(3))
            explicit_dates.append((index, match, False))
            continue
        partial = PARTIAL_DATE_RE.match(line)
        if partial and inherited_year:
            explicit_dates.append((index, partial, True))

    records = []
    for occurrence_index, (line_index, match, partial) in enumerate(explicit_dates):
        year = inherited_year if partial else int(match.group(3))
        month = MONTHS[match.group(1).title()]
        day = int(match.group(2))
        try:
            event_date = date(year, month, day).isoformat()
        except ValueError:
            continue

        time_offset = 3 if partial else 4
        time_from = parse_time(match.group(time_offset), match.group(time_offset + 1), match.group(time_offset + 2))
        next_index = explicit_dates[occurrence_index + 1][0] if occurrence_index + 1 < len(explicit_dates) else len(lines)
        following = lines[line_index + 1:next_index]

        occurrence_title = title
        venue = None
        if partial and following:
            immediate_venue = TIME_SUFFIX_RE.search(following[0])
            if immediate_venue:
                venue = sensible_venue(following[0][:immediate_venue.start()])
                if time_from is None:
                    time_from = parse_time(*immediate_venue.groups())
                if len(following) > 1:
                    occurrence_title = following[1]
                following = []
            elif len(following) > 1 and ADDRESS_RE.search(following[1]):
                venue = sensible_venue(following[0])
                previous = lines[line_index - 1] if line_index else ''
                if previous and not parse_location(previous):
                    occurrence_title = previous
                following = []
            else:
                occurrence_title = following[0]
                following = following[1:]
        if following:
            if parse_location(following[0]):
                following = following[1:]
            venue_line = following[0] if following else ''
            for candidate_index, candidate in enumerate(following):
                if candidate_index and ADDRESS_RE.search(candidate):
                    venue_line = following[candidate_index - 1]
                    break
            suffix = TIME_SUFFIX_RE.search(venue_line)
            if suffix:
                venue = sensible_venue(venue_line[:suffix.start()])
                if time_from is None:
                    time_from = parse_time(*suffix.groups())
            else:
                venue = sensible_venue(venue_line)

        city, country_code = default_location
        if venue is None and line_index > 0 and not parse_location(lines[line_index - 1]):
            venue = sensible_venue(lines[line_index - 1])
        if venue is None:
            continue

        records.append({
            'title': occurrence_title,
            'date': event_date,
            'url': f'{page_url}#{element.get("id", "events")}',
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class SamiJunnonenComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='samijunnonen_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for page_url in ARCHIVE_URLS:
            try:
                response = session.get(page_url, timeout=60)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Sami Junnonen event archive',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.text, 'html.parser')
            for element in soup.select('[data-testid="richTextElement"]'):
                records.extend(parse_listing(element, page_url))

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    SamiJunnonenComCrawler().run()


if __name__ == '__main__':
    main()
