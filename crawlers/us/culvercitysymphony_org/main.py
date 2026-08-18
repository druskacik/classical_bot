import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.culvercitysymphony.org/'
SOURCE = 'Culver City Symphony Orchestra'
CALENDAR_URL = f'{SOURCE_URL}calendar-of-events.html'

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
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(\d{4})'
    r'(?:,?\s+(\d{1,2}(?::\d{2})?\s*[ap]m))?',
    re.IGNORECASE,
)

# The site's archive index is empty in the rendered page, but these concert pages
# remain published in its first-party sitemap. Multi-event overview pages are
# deliberately represented here as separate concrete performances.
ARCHIVE_EVENTS = [
    (
        'concert-february-16-2020-300pm-bull-winter-20--20-years-in-culver-city.html',
        'A Winter’s Concert — 20 Years in Culver City',
        [('2020-02-16', '15:00')],
        'Robert Frost Auditorium',
        'Culver City',
    ),
    (
        'concert-october-19-2019-730pm-bull-autumn-20--20-years-in-culver-city.html',
        'Autumn 20 — 20 Years in Culver City',
        [('2019-10-19', '19:30')],
        'Robert Frost Auditorium',
        'Culver City',
    ),
    (
        'marina-concerts-2022.html',
        'Opera at the Shore',
        [('2022-07-14', '19:00')],
        'Burton Chace Park',
        'Marina del Rey',
    ),
    (
        'marina-concerts-2022.html',
        'A Night Celebrating Music in Film',
        [('2022-08-04', '19:00')],
        'Burton Chace Park',
        'Marina del Rey',
    ),
    (
        '1-marina-concerts-la-boheme-and-phantom-of-the-opera.html',
        'La Bohème and Phantom of the Opera',
        [('2019-07-11', None)],
        'Burton Chace Park',
        'Marina del Rey',
    ),
    (
        '2-marina-concerts-dance-in-america.html',
        'Sights, Sounds & Dance in America',
        [('2019-07-25', None)],
        'Burton Chace Park',
        'Marina del Rey',
    ),
    (
        '3-marina-concerts-kiss-me-kate.html',
        'Kiss Me, Kate',
        [('2019-08-22', None), ('2019-08-24', None)],
        'Burton Chace Park',
        'Marina del Rey',
    ),
]


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def page_text(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('#main-wrap') or soup.select_one('main') or soup.body
    return clean_text(content.get_text('\n', strip=True) if content else '')


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'(?<=\d)(AM|PM)$', r' \1', value.strip().upper())
    normalized = re.sub(r'\s+', ' ', normalized)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def current_event(text):
    match = DATE_RE.search(text)
    if not match:
        return None

    month, day, year, event_time = match.groups()
    try:
        event_date = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None

    before = text[max(0, match.start() - 250):match.start()]
    after_lines = [line.strip(' \u200b') for line in text[match.end():].splitlines() if line.strip(' \u200b')]
    title_lines = [
        line for line in after_lines[:2]
        if len(line) >= 8
        and not re.match(r'^(free admission|tickets?|presented at)', line, re.I)
    ]
    title = ' '.join(title_lines)
    if not title:
        return None

    if 'Burton Chace Park' in before:
        venue, city = 'Burton Chace Park', 'Marina del Rey'
    elif 'Robert Frost Auditorium' in before:
        venue, city = 'Robert Frost Auditorium', 'Culver City'
    elif 'Veterans' in before and 'Auditorium' in before:
        venue, city = 'Veterans Memorial Auditorium', 'Culver City'
    else:
        return None

    return title, event_date, parse_time(event_time), venue, city


def make_record(title, event_date, event_time, venue, city, url, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    description = page_text(response)
    event = current_event(description)
    if event:
        records.append(make_record(*event, CALENDAR_URL, description))

    descriptions = {}
    for slug, title, dates, venue, city in ARCHIVE_EVENTS:
        url = f'{SOURCE_URL}{slug}'
        if url not in descriptions:
            try:
                archive_response = session.get(url, timeout=45)
                archive_response.raise_for_status()
                descriptions[url] = page_text(archive_response)
            except requests.RequestException as error:
                log_message(
                    'Archived concert page request failed',
                    event='crawler_archive_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
        for event_date, event_time in dates:
            records.append(
                make_record(title, event_date, event_time, venue, city, url, descriptions[url])
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class CulverCitySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='culvercitysymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CulverCitySymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
