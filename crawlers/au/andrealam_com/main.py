import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.andrealam.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'new-events')
SOURCE = 'Andrea Lam'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-AU,en;q=0.9',
}

# The Squarespace events do not expose a location field.  The editor instead
# writes venue names into the event body.  These are stable, unambiguous venues
# represented in the published calendar, including Andrea Lam's tour dates.
LOCATIONS = (
    ('Blue Mountains Theatre', 'Blue Mountains Theatre', 'Springwood', 'AU'),
    ('Melbourne Recital Centre', 'Melbourne Recital Centre', 'Melbourne', 'AU'),
    ('Opera House, Concert Hall', 'Sydney Opera House Concert Hall', 'Sydney', 'AU'),
    ('Sydney Opera House', 'Sydney Opera House Concert Hall', 'Sydney', 'AU'),
    ('Hamer Hall', 'Hamer Hall', 'Melbourne', 'AU'),
    ('Costa Hall', 'Costa Hall', 'Geelong', 'AU'),
    ('UKARIA', 'UKARIA Cultural Centre', 'Mount Barker', 'AU'),
    ('Hunters Hill Town Hall', 'Hunters Hill Town Hall', 'Hunters Hill', 'AU'),
    ('Beleura', 'Beleura House & Garden', 'Mornington', 'AU'),
    ('Windsong Pavilion', 'Windsong Pavilion', 'Barragga Bay', 'AU'),
    ('Dunkeld', 'Dunkeld Community Centre', 'Dunkeld', 'AU'),
    ('Castlemaine Town Hall', 'Castlemaine Town Hall', 'Castlemaine', 'AU'),
    ('Clancy Auditorium', 'Sir John Clancy Auditorium', 'Sydney', 'AU'),
    ('Auckland Town Hall', 'Auckland Town Hall', 'Auckland', 'NZ'),
    ('City Recital Hall', 'City Recital Hall', 'Sydney', 'AU'),
    ('Market City', 'Market City', 'Sydney', 'AU'),
    ('Sydney Metro', 'Sydney Metro', 'Sydney', 'AU'),
    ('Central Station', 'Central Station', 'Sydney', 'AU'),
)

MONTHS = {
    name.casefold(): number for number, name in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    ) if name
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_location(title, description):
    combined = f'{title}\n{description}'
    for needle, venue, city, country_code in LOCATIONS:
        if needle.casefold() in combined.casefold():
            return venue, city, country_code

    # These orchestra-specific entries omit the venue, but link to concrete
    # performances in their normal home concert halls.
    lowered = title.casefold()
    if 'west australian symphony orchestra' in lowered:
        return 'Perth Concert Hall', 'Perth', 'AU'
    if 'adelaide symphony orchestra' in lowered:
        return 'Adelaide Town Hall', 'Adelaide', 'AU'
    if 'grand teton music festival orchestra' in lowered:
        return 'Walk Festival Hall', 'Teton Village', 'US'
    if 'bendigo chamber music festival' in lowered:
        return 'The Capital', 'Bendigo', 'AU'
    if 'four winds easter concert' in lowered:
        return 'Windsong Pavilion', 'Barragga Bay', 'AU'
    return None


def parse_time(value, default=None):
    match = re.search(r'\b(\d{1,2})(?::([0-5]\d))?\s*([ap]m)\b', value, re.I)
    if not match:
        match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
        if not match:
            return None
        hour = int(match.group(1))
        if hour <= 12 and default and int(default[:2]) >= 12:
            hour = hour % 12 + 12
        return f'{hour:02d}:{match.group(2)}'
    hour = int(match.group(1)) % 12
    if match.group(3).casefold() == 'pm':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def extract_occurrences(title, description, default_date, default_time, default_location):
    """Extract separately advertised performances from free-form body lines."""
    year = int(default_date[:4])
    occurrences = []
    patterns = (
        # July 16, 7:30 Hamer Hall
        re.compile(
            r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2}),\s*'
            r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(.*)$', re.I
        ),
        # Wed 03 September, 8pm
        re.compile(
            r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+(\d{1,2})\s+('
            + '|'.join(MONTHS) + r'),\s*'
            r'(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*(.*)$', re.I
        ),
    )
    for line in description.splitlines():
        match = patterns[0].search(line)
        if match:
            month_name, day_text, time_text, location_text = match.groups()
        else:
            match = patterns[1].search(line)
            if not match:
                continue
            day_text, month_name, time_text, location_text = match.groups()
        try:
            occurrence_date = date(
                year, MONTHS[month_name.casefold()], int(day_text)
            ).isoformat()
        except ValueError:
            continue
        location = parse_location(title, location_text) or default_location
        if location:
            occurrences.append((occurrence_date, parse_time(time_text, default_time), location))

    # Preserve order while removing repeated body lines.
    return list(dict.fromkeys(occurrences))


def parse_article(article):
    title_link = article.select_one('a.eventlist-title-link[href]')
    date_element = article.select_one('.eventlist-meta-date time[datetime]')
    if title_link is None or date_element is None:
        return []

    title = clean_text(title_link)
    event_date = date_element.get('datetime', '').strip()
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except ValueError:
        return []

    description = clean_text(article.select_one('.eventlist-description'))
    location = parse_location(title, description)
    if not title or not location:
        return []

    time_element = article.select_one(
        '.eventlist-meta-time .event-time-24hr-start, '
        '.eventlist-meta-time time.event-time-24hr'
    )
    default_time = parse_time(clean_text(time_element))
    occurrences = extract_occurrences(
        title, description, event_date, default_time, location
    )
    if not occurrences:
        occurrences = [(event_date, default_time, location)]

    records = []
    for occurrence_date, occurrence_time, occurrence_location in occurrences:
        venue, city, country_code = occurrence_location
        records.append({
            'title': title,
            'date': occurrence_date,
            'url': urljoin(SOURCE_URL, title_link['href']),
            'time_from': occurrence_time,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class AndreaLamComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='andrealam_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AU',
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

    def scrape(self):
        try:
            response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Andrea Lam calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for article in soup.select('article.eventlist-event'):
            records.extend(parse_article(article))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    AndreaLamComCrawler().run()


if __name__ == '__main__':
    main()
