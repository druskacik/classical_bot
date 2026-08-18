import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.luzernemusic.org/'
SEASON_URL = f'{SOURCE_URL}2026-season'
SOURCE = 'Luzerne Music Center'
VENUE = 'Luzerne Music Center'
CITY = 'Lake Luzerne'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

FULL_DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*[•,]\s*'
    r'([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s*•\s*'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)
STUDENT_DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+)\s+(\d{1,2})\s*[–-]\s*'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)'
    r'(?:\s*\(([^)]+)\))?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(month, day, year):
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = clean_text(value).upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(value.replace(' ', ''), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def record(title, event_date, time_from, description=None):
    return {
        'title': title,
        'date': event_date,
        'url': SEASON_URL,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_paid_concerts(elements):
    records = []
    for index, text in enumerate(elements):
        match = FULL_DATE_RE.fullmatch(text)
        if not match or index == 0:
            continue

        title = elements[index - 1]
        if title == 'Musicians of The Philadelphia Orchestra' and index >= 2:
            title = f'{elements[index - 2]}: {title}'
        if not title or title.lower() == 'buy tickets':
            continue
        month, day, year, event_time = match.groups()
        event_date = parse_date(month, day, year)
        if not event_date:
            continue

        description_parts = []
        for following_index in range(index + 1, len(elements)):
            following = elements[following_index]
            next_is_date = (
                following_index + 1 < len(elements)
                and FULL_DATE_RE.fullmatch(elements[following_index + 1])
            )
            next_two_is_date = (
                following_index + 2 < len(elements)
                and FULL_DATE_RE.fullmatch(elements[following_index + 2])
            )
            if FULL_DATE_RE.fullmatch(following) or next_is_date or next_two_is_date:
                break
            if following in {'FACULTY ARTIST SERIES', 'FREE STUDENT CONCERTS'}:
                break
            if following.lower() == 'buy tickets':
                continue
            if following not in description_parts:
                description_parts.append(following)

        records.append(record(
            title,
            event_date,
            parse_time(event_time),
            '\n\n'.join(description_parts) or None,
        ))
    return records


def parse_student_concerts(elements, year):
    records = []
    for index, text in enumerate(elements):
        matches = list(STUDENT_DATE_RE.finditer(text))
        if not matches or index == 0:
            continue

        series = elements[index - 1]
        if series not in {'Student Showcases', 'Piano Prelude & LMC Symphony Orchestra'}:
            continue
        for match in matches:
            month, day, event_time, subtype = match.groups()
            event_date = parse_date(month, day, year)
            if not event_date:
                continue
            title = f'{series}: {subtype}' if subtype else series
            records.append(record(
                title,
                event_date,
                parse_time(event_time),
                'Free student performance, open to the public.',
            ))
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SEASON_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    elements = []
    for node in soup.select('.wixui-rich-text'):
        text = clean_text(node.get_text(' ', strip=True))
        if text and text not in elements[-1:]:
            elements.append(text)

    year_match = re.search(r'\b(20\d{2})\s+SEASON\b', ' '.join(elements), re.IGNORECASE)
    if not year_match:
        log_message(
            'Season year not found',
            event='crawler_parse_warning',
            level='warning',
            url=SEASON_URL,
            error_type='MissingSeasonYear',
        )
        return []

    records = parse_paid_concerts(elements)
    records.extend(parse_student_concerts(elements, year_match.group(1)))
    records.sort(key=lambda item: (item['date'], item['time_from'] or '', item['title']))

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SEASON_URL,
            record_count=0,
        )
    return records


class LuzerneMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='luzernemusic_org',
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
        return scrape_concerts()


def main():
    LuzerneMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
