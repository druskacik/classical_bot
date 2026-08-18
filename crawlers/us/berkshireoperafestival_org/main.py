import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.berkshireoperafestival.org/'
SOURCE = 'Berkshire Opera Festival'
SEASON_URL = f'{SOURCE_URL}season'
ARCHIVE_URL = f'{SOURCE_URL}previous-seasons'
RESIDENT_URL = f'{SOURCE_URL}residentartist'
DETAIL_URLS = [
    f'{SOURCE_URL}luciadilammermoor',
    f'{SOURCE_URL}zemireetazor',
    f'{SOURCE_URL}winterreise',
]
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
DATE_LINE_RE = re.compile(
    r'^(?:(?:Mon|Tues?|Wed|Thurs?|Fri|Sat|Sun)\.\s+)?'
    r'([A-Z][a-z]{2})\.\s+(\d{1,2}),\s+(\d{1,2}:\d{2})\s*([AP]M)$'
)
RESIDENT_RE = re.compile(
    r'([^\n]+)\n(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Z][a-z]+)\s+(\d{1,2})\s+[•|]\s+(\d{1,2}:\d{2})\s*([AP]M)\n'
    r'([^\n]+),\s*([^,\n]+),\s*(MA|NY)\b'
)
ARCHIVE_DATE_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(\d{1,2}(?:\s*(?:,|&)\s*\d{1,2})*)$'
)


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def page_lines(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    return [clean_text(line) for line in soup.get_text('\n').splitlines() if clean_text(line)]


def description_from_lines(lines):
    excluded = {
        'top of page', 'DONATE NOW', 'Log In', 'SEASON', 'TICKETS', 'ARTISTS',
        'SUBSCRIBE', 'FOLLOW US', 'Privacy Policy', 'Settings', 'Close',
    }
    values = []
    for line in lines:
        if line not in excluded and line not in values:
            values.append(line)
    return '\n'.join(values) or None


def parse_time(value, meridiem):
    return datetime.strptime(f'{value} {meridiem}', '%I:%M %p').strftime('%H:%M')


def parse_detail(response, year):
    lines = page_lines(response)
    location_label = next((i for i, line in enumerate(lines) if line == 'LOCATION'), None)
    date_label = next((i for i, line in enumerate(lines) if line in {'DATE', 'DATES'}), None)
    if location_label is None or date_label is None or date_label <= location_label + 1:
        return []
    title = next(
        (line.title() for line in reversed(lines[:location_label])
         if line.upper() == line and len(line) > 4 and not line.startswith('(') and line not in {
             'COMPOSER', 'CREATORS', 'CONCERT', 'TICKETS', 'MAINSTAGE PRODUCTION',
             'RESIDENT ARTIST PRODUCTION', 'SEASON', 'DONATE NOW',
         }),
        '',
    )
    if not title:
        return []

    venue = lines[location_label + 1]
    address = ' '.join(lines[location_label + 2:date_label])
    city_match = re.search(r',\s*([^,]+),\s*(?:MA|NY)\b', address)
    if not city_match:
        return []
    city = clean_text(city_match.group(1))
    records = []
    for line in lines[date_label + 1:date_label + 6]:
        match = DATE_LINE_RE.match(line)
        if not match:
            continue
        month, day, time_value, meridiem = match.groups()
        try:
            event_date = datetime.strptime(f'{month} {day} {year}', '%b %d %Y').date().isoformat()
        except ValueError:
            continue
        records.append(make_record(
            title, event_date, response.url, parse_time(time_value, meridiem),
            venue, city, description_from_lines(lines),
        ))
    return records


def make_record(title, event_date, url, time_from, venue, city, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_resident_events(response, year):
    text = '\n'.join(page_lines(response))
    records = []
    for match in RESIDENT_RE.finditer(text):
        title, month, day, time_value, meridiem, venue, city, _state = match.groups()
        try:
            event_date = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
        except ValueError:
            continue
        records.append(make_record(
            clean_text(title), event_date, response.url,
            parse_time(time_value, meridiem), clean_text(venue), clean_text(city),
            description_from_lines(page_lines(response)),
        ))
    return records


def parse_archive(response):
    """Parse the first-party season archive, whose entries have no detail URLs."""
    lines = page_lines(response)
    records = []
    year = None
    for index, line in enumerate(lines):
        if re.fullmatch(r'20\d{2}', line):
            year = int(line)
            continue
        match = ARCHIVE_DATE_RE.match(line.replace('\xa0', ' '))
        if not year or not match:
            continue
        if index and ARCHIVE_DATE_RE.match(lines[index - 1].replace('\xa0', ' ')):
            continue

        month, days_text = match.groups()
        location_index = None
        city = venue = ''
        extra_date = ARCHIVE_DATE_RE.match(lines[index + 1]) if index + 1 < len(lines) else None
        location_start = index + 2 if extra_date else index + 1
        for candidate_index in range(location_start, min(index + 5, len(lines))):
            location = re.search(r'^(.*?)(?:,\s*)?([^,]+),\s*(MA|NY)$', lines[candidate_index])
            if location:
                location_index = candidate_index
                city = clean_text(location.group(2))
                venue_parts = lines[location_start:candidate_index]
                leading_venue = clean_text(location.group(1))
                if leading_venue:
                    venue_parts.append(leading_venue)
                venue = ', '.join(dict.fromkeys(venue_parts))
                break
        if location_index is None or not city or not venue:
            continue

        title = ''
        for candidate in reversed(lines[max(0, index - 6):index]):
            if candidate == 'Out of gallery' or candidate.startswith(('by ', 'Music by ', 'A ', 'Spring ')):
                continue
            if candidate.upper() == candidate and re.search(r'[A-Z]', candidate):
                title = clean_text(candidate)
                break
        if not title:
            continue

        description = '\n'.join(lines[max(0, index - 6):min(len(lines), location_index + 12)])
        for day in re.findall(r'\d{1,2}', days_text):
            try:
                event_date = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
            except ValueError:
                continue
            records.append(make_record(
                title.title(), event_date, response.url, None, venue, city, description,
            ))
        if extra_date:
            extra_month, extra_days = extra_date.groups()
            for day in re.findall(r'\d{1,2}', extra_days):
                try:
                    event_date = datetime.strptime(
                        f'{extra_month} {day} {year}', '%B %d %Y'
                    ).date().isoformat()
                except ValueError:
                    continue
                records.append(make_record(
                    title.title(), event_date, response.url, None, venue, city, description,
                ))
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    year = datetime.now().year
    records = []
    for url in DETAIL_URLS:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        records.extend(parse_detail(response, year))

    response = session.get(RESIDENT_URL, timeout=45)
    response.raise_for_status()
    records.extend(parse_resident_events(response, year))

    response = session.get(ARCHIVE_URL, timeout=45)
    response.raise_for_status()
    records.extend(parse_archive(response))

    if not records:
        log_message(
            'No concert occurrences found', event='crawler_empty_listing',
            level='warning', url=SEASON_URL, record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BerkshireOperaFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='berkshireoperafestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BerkshireOperaFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
