import html
import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.paularobison.com/'
EVENTS_URL = f'{SOURCE_URL}upcoming-events'
ARCHIVE_URL = f'{SOURCE_URL}previous-events'
SOURCE = 'Paula Robison'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

# The old hand-written archive is consistent about the cities it uses, but not
# about addresses.  These mappings let us retain only entries whose geography
# and venue can be established from the page itself.
CITY_COUNTRIES = {
    'Ann Arbor': 'US', 'Baltimore': 'US', 'Boston': 'US', 'Boulder': 'US',
    'Cambridge': 'US', 'Charleston': 'US', 'Chestnut Hill': 'US',
    'Fairport': 'US', 'Harrisonburg': 'US', 'Houston': 'US', 'Hudson': 'US',
    'Interlochen': 'US', 'Jamaica Plain': 'US', 'Marblehead': 'US',
    'Miami': 'US', 'Miami Beach': 'US', 'New York': 'US', 'Newburyport': 'US',
    'Oberlin': 'US', 'Orlando': 'US', 'Rochester': 'US', 'Sarasota': 'US',
    'Scarsdale': 'US', 'Woodstock': 'US', 'Denver': 'US',
    'Toronto': 'CA', 'Orford': 'CA',
}

KNOWN_VENUES = (
    'Jordan Hall', 'Williams Hall', 'Brown Hall', 'Calderwood Hall',
    'Elebash Recital Hall', 'Alice Tully Hall', 'Sanders Theater',
    'Sanders Theatre', 'Hatch Recital Hall', 'King Center Concert Hall',
    'King Center Recital Hall', 'New World Center', 'New World Center',
    'New England Conservatory', 'Isabella Stewart Gardner Museum',
    'The Graduate Center, CUNY', 'Austrian Cultural Forum',
    'Saint Bartholomew\u2019s Church', "St Johns Church", 'The Barge',
    'Christ Church Episcopal', 'Wertheim Auditorium', 'Abbot Hall',
    'Firehouse Center for the Arts', 'College of Charleston',
    'Church of the Redeemer', "NEC's Jordan Hall", 'Flutistry Boston',
    'Mannes College / The New School for Music',
)

DATE_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|October|November|December) '
    r'(\d{1,2})(?:st|nd|rd|th)?[,]? (\d{4})(?:\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m))?$',
    re.I,
)


def clean_text(value):
    if not value:
        return None
    text = html.unescape(BeautifulSoup(value, 'html.parser').get_text('\n', strip=True))
    text = text.replace('\xa0', ' ').replace('\u2028', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def fetch_json(url):
    response = requests.get(url, params={'format': 'json'}, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response.json()


def city_country(text):
    for city in sorted(CITY_COUNTRIES, key=len, reverse=True):
        if re.search(rf'(?<!\w){re.escape(city)}(?!\w)', text, re.I):
            return city, CITY_COUNTRIES[city]
    return None, None


def venue_from_text(text):
    for venue in KNOWN_VENUES:
        if venue.casefold() in text.casefold():
            return venue
    return None


def parse_time(text):
    if not text:
        return None
    match = re.search(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', text, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == 'p' else 0)
    return f'{hour:02d}:{match.group(2) or "00"}'


def structured_record(item, timezone):
    title = str(item.get('title') or '').strip()
    path = str(item.get('fullUrl') or '').strip()
    location = item.get('location') or {}
    description = clean_text(item.get('body') or item.get('excerpt'))
    location_text = ' '.join(
        str(location.get(key) or '')
        for key in ('addressTitle', 'addressLine1', 'addressLine2', 'addressCountry')
    )
    evidence = f'{location_text}\n{description or ""}\n{title}'
    city, country = city_country(evidence)
    country_name = str(location.get('addressCountry') or '').casefold()
    if country_name in ('united states', 'usa'):
        country = 'US'
    elif country_name == 'canada':
        country = 'CA'
    venue = html.unescape(str(location.get('addressTitle') or '')).strip() or venue_from_text(evidence)
    try:
        start = datetime.fromtimestamp(int(item['startDate']) / 1000, ZoneInfo(timezone))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all((title, path, venue, city, country)):
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': requests.compat.urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def archive_records(html):
    soup = BeautifulSoup(html or '', 'html.parser')
    elements = [tag for tag in soup.find_all(['h1', 'h2', 'p']) if clean_text(str(tag))]
    records = []
    for index, element in enumerate(elements):
        date_match = DATE_RE.fullmatch(clean_text(str(element)) or '')
        if not date_match:
            continue
        following = []
        for tag in elements[index + 1:]:
            text = clean_text(str(tag)) or ''
            if DATE_RE.fullmatch(text) or (tag.name == 'h2' and 'Season' in text):
                break
            following.append((tag.name, text))
        title = next((text for name, text in following if name == 'h1'), None)
        body_parts = [text for name, text in following if name == 'p']
        evidence = '\n'.join(([title] if title else []) + body_parts)
        venue = venue_from_text(evidence)
        city, country = city_country(evidence)
        if not all((title, venue, city, country)):
            continue
        parsed_date = datetime.strptime(
            f'{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}', '%B %d %Y'
        ).date().isoformat()
        explicit_time = None
        if date_match.group(4):
            explicit_time = (
                f'{date_match.group(4)}:{date_match.group(5) or "00"} {date_match.group(6)}'
            )
        time_from = parse_time(explicit_time) or parse_time(evidence)
        fragment = quote(f'{parsed_date}-{title.casefold().replace(" ", "-")}', safe='-')
        records.append({
            'title': title,
            'date': parsed_date,
            'url': f'{ARCHIVE_URL}#{fragment}',
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country,
            'description': '\n'.join(body_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class PaulaRobisonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='paularobison_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        events_payload = fetch_json(EVENTS_URL)
        timezone = (events_payload.get('website') or {}).get('timeZone') or 'America/Denver'
        records = []
        for item in events_payload.get('upcoming', []) + events_payload.get('past', []):
            record = structured_record(item, timezone)
            if record:
                records.append(record)

        try:
            archive_payload = fetch_json(ARCHIVE_URL)
            records.extend(archive_records(archive_payload.get('mainContent')))
        except (requests.RequestException, ValueError, TypeError) as error:
            log_message(
                'Failed to scrape historical events archive',
                event='crawler_archive_failed',
                level='warning',
                url=ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    PaulaRobisonComCrawler().run()


if __name__ == '__main__':
    main()
