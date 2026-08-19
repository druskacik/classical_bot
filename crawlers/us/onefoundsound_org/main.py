import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.onefoundsound.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'One Found Sound'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(20\d{2})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(A\.?M\.?|P\.?M\.?)\b', re.IGNORECASE)
STATE_RE = re.compile(r'^[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$')


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def element_lines(element):
    copy = BeautifulSoup(str(element), 'html.parser')
    for br in copy.find_all('br'):
        br.replace_with('\n')
    return [clean_text(line) for line in copy.get_text('\n').splitlines() if clean_text(line)]


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    normalized = f'{match.group(1)}:{match.group(2) or "00"} {match.group(3).replace(".", "").upper()}'
    try:
        return datetime.strptime(normalized, '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None


def parse_location(value):
    parts = [clean_text(part) for part in value.split(',') if clean_text(part)]
    if len(parts) < 3:
        return None

    state_index = next(
        (index for index in range(len(parts) - 1, 0, -1) if STATE_RE.fullmatch(parts[index])),
        None,
    )
    if state_index is None or state_index < 2:
        return None

    venue = parts[0]
    city = parts[state_index - 1]
    if city.upper() == 'SF':
        city = 'San Francisco'
    if not venue or not city:
        return None
    return venue, city


def parse_event_paragraph(paragraph):
    lines = element_lines(paragraph)
    date_index = next((index for index, line in enumerate(lines) if DATE_RE.search(line)), None)
    if date_index is None or date_index == 0 or date_index + 1 >= len(lines):
        return None

    event_date = parse_date(lines[date_index])
    location = parse_location(lines[date_index + 1])
    if not event_date or not location:
        return None

    title = lines[0]
    venue, city = location
    excluded = {title, lines[date_index], lines[date_index + 1]}
    description_lines = [
        line for line in lines
        if line not in excluded
        and line.lower() not in {
            'click to view the program',
            'haz clic aquí para ver el programa en español',
        }
    ]

    sibling = paragraph.find_next_sibling('p')
    if sibling is not None:
        sibling_text = clean_text(sibling.get_text(' ', strip=True))
        if sibling_text and not sibling_text.lower().startswith('all photos by'):
            description_lines.append(sibling_text)

    program_link = paragraph.select_one('a[href*="program"]')
    url = urljoin(EVENTS_URL, program_link['href']) if program_link else EVENTS_URL

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(lines[date_index]),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n'.join(description_lines) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OneFoundSoundOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='onefoundsound_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch One Found Sound events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for paragraph in soup.select('p[data-rte-preserve-empty]'):
            record = parse_event_paragraph(paragraph)
            if record:
                records.append(record)

        if not records:
            log_message(
                'No One Found Sound events found',
                event='crawler_empty_listing',
                level='warning',
                url=EVENTS_URL,
                record_count=0,
            )

        return sorted(
            records,
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    OneFoundSoundOrgCrawler().run()


if __name__ == '__main__':
    main()
