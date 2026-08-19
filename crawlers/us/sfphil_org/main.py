import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sfphil.org/'
UPCOMING_URL = f'{SOURCE_URL}upcoming-performances-1'
PAST_URL = f'{SOURCE_URL}past-performances'
SOURCE = 'San Francisco Philharmonic'
CITY = 'San Francisco'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|'
    'October|November|December'
)
HEADING_RE = re.compile(rf'^({MONTHS})\s+(\d{{4}})$', re.I)
DATE_RE = re.compile(
    rf'(?:(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    rf'({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?'
    rf'(?:\s*(?:[-–—,]\s*)?(\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)))?',
    re.I,
)
VENUES = (
    (re.compile(r"Saint Joseph[’']s Arts Society", re.I), "Saint Joseph's Arts Society"),
    (re.compile(r'Herbst Theat(?:re|er)', re.I), 'Herbst Theatre'),
    (re.compile(r'Wilsey Center', re.I), 'Wilsey Center'),
    (re.compile(r'Fisk Mansion(?:,\s*700 Hayes Street)?', re.I), 'Fisk Mansion'),
    (re.compile(r'Local Edition on Market\s*&\s*3rd Stree\s*t', re.I), 'Local Edition'),
    (re.compile(r'712 Steiner St', re.I), 'Blue Painted Lady'),
)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u202f', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def section_parts(section):
    return [
        clean_text(element.get_text(' ', strip=True))
        for element in section.select('h1, h2, h3, p')
        if clean_text(element.get_text(' ', strip=True))
    ]


def resolve_venue(text):
    for pattern, venue in VENUES:
        match = pattern.search(text)
        if match:
            return venue, match
    return None, None


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'\s+', '', value).upper()
    if ':' not in normalized:
        normalized = re.sub(r'(?=[AP]M$)', ':00', normalized)
    try:
        return datetime.strptime(normalized, '%I:%M%p').strftime('%H:%M')
    except ValueError:
        return None


def parse_section(section, page_url):
    parts = section_parts(section)
    records = []
    current_heading = None

    for index, part in enumerate(parts):
        heading = HEADING_RE.fullmatch(part)
        if heading:
            current_heading = heading
            continue

        matches = list(DATE_RE.finditer(part))
        for match_index, match in enumerate(matches):
            year = int(match.group(4) or (current_heading.group(2) if current_heading else 0))
            if not year:
                continue
            try:
                event_date = datetime.strptime(
                    f'{match.group(2)} {match.group(3)} {year}', '%B %d %Y'
                ).date()
            except ValueError:
                continue

            weekday = match.group(1)
            if weekday and event_date.strftime('%A').lower() != weekday.lower():
                log_message(
                    'Skipping concert with conflicting weekday and date',
                    event='crawler_item_skipped',
                    level='warning',
                    url=page_url,
                    date=event_date.isoformat(),
                )
                continue

            prefix = clean_text(part[:match.start()])
            if prefix:
                title = prefix
            else:
                title = next(
                    (
                        parts[position]
                        for position in range(index - 1, -1, -1)
                        if not HEADING_RE.fullmatch(parts[position])
                    ),
                    '',
                )

            boundary = matches[match_index + 1].start() if match_index + 1 < len(matches) else len(part)
            remainder = clean_text(part[match.end():boundary])
            venue, venue_match = resolve_venue(remainder)
            if not venue:
                # Squarespace sometimes keeps the venue in the following paragraph.
                following = parts[index + 1] if index + 1 < len(parts) else ''
                venue, venue_match = resolve_venue(following)
                remainder = f'{remainder} {following}'.strip()
            if not title or not venue:
                continue

            description_parts = []
            if venue_match:
                tail = clean_text(remainder[venue_match.end():])
                if tail:
                    description_parts.append(tail)
            for following in parts[index + 1:]:
                if HEADING_RE.fullmatch(following) or DATE_RE.search(following):
                    break
                if following not in description_parts:
                    description_parts.append(following)

            records.append({
                'title': title,
                'date': event_date.isoformat(),
                'url': page_url,
                'time_from': parse_time(match.group(5)),
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': clean_text(' '.join(description_parts)) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page_url in (UPCOMING_URL, PAST_URL):
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for section in soup.select('main section.page-section'):
            records.extend(parse_section(section, page_url))
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class SfphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfphil_org',
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
        return get_concerts()


def main():
    SfphilOrgCrawler().run()


if __name__ == '__main__':
    main()
