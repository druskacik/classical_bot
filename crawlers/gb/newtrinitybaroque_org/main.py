import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.newtrinitybaroque.org/'
SOURCE = 'New Trinity Baroque'
WIX_URL = 'https://pgosta.wixsite.com/newtrinitybaroque'
CONCERTS_URL = f'{WIX_URL}/concerts'

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
            '', 'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        )
    )
    if name
}

LOCATIONS = {
    'church of st john the baptist': ('Church of St John the Baptist', 'London', 'GB'),
    'st john the baptist': ('St John the Baptist', 'London', 'GB'),
    'madlenianum opera & theatre': ('Madlenianum Opera & Theatre', 'Belgrade', 'RS'),
    'church of st bartholomew the great': (
        'Church of St Bartholomew the Great', 'London', 'GB'
    ),
    'christ church': ('Christ Church', 'St Leonards-on-Sea', 'GB'),
    'st george\'s hanover square': ('St George\'s Hanover Square', 'London', 'GB'),
    'holy trinity church': ('Holy Trinity Church', 'Minchinhampton', 'GB'),
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '').replace('\ufeff', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def rich_text_blocks(html):
    soup = BeautifulSoup(html, 'html.parser')
    return [
        text for element in soup.select('[data-testid="richTextElement"]')
        if (text := clean_text(element))
    ]


def extract_dates(header):
    matches = list(re.finditer(
        r'(?<!\d)(\d{1,2})\s+(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)(?:\s+(20\d{2}))?',
        header,
        re.IGNORECASE,
    ))
    if not matches:
        return []
    years = [match.group(3) for match in matches if match.group(3)]
    if not years:
        return []
    fallback_year = int(years[-1])
    results = []
    for match in matches:
        try:
            event_date = date(
                int(match.group(3) or fallback_year),
                MONTHS[match.group(2).lower()],
                int(match.group(1)),
            ).isoformat()
        except ValueError:
            continue
        tail = header[match.end():matches[matches.index(match) + 1].start()] \
            if matches.index(match) + 1 < len(matches) else header[match.end():]
        time_match = re.search(r'\b(?:at\s*)?([01]?\d|2[0-3])[:.]([0-5]\d)\b', tail)
        results.append((event_date, f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None))
    return results


def resolve_locations(header, occurrence_count):
    found = []
    lowered = header.lower()
    for marker, location in LOCATIONS.items():
        position = lowered.find(marker)
        if position >= 0:
            found.append((position, location))
    found.sort(key=lambda item: item[0])
    locations = [location for _, location in found]
    if not locations:
        return []
    if occurrence_count == 1:
        return [locations[0]]
    if len(locations) == 1:
        return locations * occurrence_count
    return locations if len(locations) == occurrence_count else []


def description_text(parts):
    text = clean_text('\n\n'.join(parts))
    text = re.sub(r'\n+(?:BOOK TICKETS|FREE ADMISSION|ADMISSION FREE[^\n]*)\s*$', '', text, flags=re.I)
    return text or None


def parse_concerts(html):
    blocks = rich_text_blocks(html)
    header_indexes = [
        index for index, block in enumerate(blocks)
        if re.match(
            r'^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?\d{1,2}\s+',
            block,
            re.IGNORECASE,
        ) and extract_dates(block)
    ]
    records = []
    for position, start in enumerate(header_indexes):
        end = header_indexes[position + 1] if position + 1 < len(header_indexes) else len(blocks) - 1
        header = blocks[start]
        occurrences = extract_dates(header)
        locations = resolve_locations(header, len(occurrences))
        title = blocks[start + 1] if start + 1 < end else ''
        if not title or not locations:
            continue
        description = description_text(blocks[start + 2:end])
        for (event_date, time_from), (venue, city, country_code) in zip(occurrences, locations):
            records.append({
                'title': title,
                'date': event_date,
                'url': CONCERTS_URL,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def parse_home_feature(html):
    text = clean_text(BeautifulSoup(html, 'html.parser'))
    match = re.search(
        r"(?P<title>HANDEL'S [^\n]+?)\s*-\s*(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|"
        r"FRIDAY|SATURDAY|SUNDAY),?\s*(?P<day>\d{1,2})\s+(?P<month>[A-Z]+)\s+"
        r"(?P<year>20\d{2}),?\s+AT\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
        r"(?P<ampm>AM|PM)\s*\n?\((?P<venue>[^,\n]+),\s*(?P<city>[^)\n]+)\)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    location = LOCATIONS.get(match.group('venue').strip().lower())
    month = MONTHS.get(match.group('month').lower())
    if not location or not month:
        return None
    try:
        event_date = date(int(match.group('year')), month, int(match.group('day'))).isoformat()
    except ValueError:
        return None
    hour = int(match.group('hour')) % 12 + (12 if match.group('ampm').upper() == 'PM' else 0)
    venue, city, country_code = location
    return {
        'title': clean_text(match.group('title')),
        'date': event_date,
        'url': WIX_URL,
        'time_from': f"{hour:02d}:{match.group('minute') or '00'}",
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class NewTrinityBaroqueOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='newtrinitybaroque_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            concerts_response = session.get(CONCERTS_URL, timeout=45)
            concerts_response.raise_for_status()
            home_response = session.get(WIX_URL, timeout=45)
            home_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch New Trinity Baroque pages',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_concerts(concerts_response.text)
        feature = parse_home_feature(home_response.text)
        if feature:
            records.append(feature)
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    NewTrinityBaroqueOrgCrawler().run()


if __name__ == '__main__':
    main()
