import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bachfestival.org/'
SOURCE = 'Carmel Bach Festival'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
SCHEDULE_SLUG = 'tickets-and-passes'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUES = {
    'AS': ('All Saints’ Episcopal Church', 'Carmel-by-the-Sea'),
    'CF': ('Church in the Forest', 'Pebble Beach'),
    'CM': ('Carmel Mission Basilica', 'Carmel-by-the-Sea'),
    'CP': ('Carmel Presbyterian Church', 'Carmel-by-the-Sea'),
    'EGL': ('El Gabilan Library', 'Salinas'),
    'ML': ('Marina Library', 'Marina'),
    'MPL': ('Monterey Public Library', 'Monterey'),
    'GM': ('Pacific Grove Museum of Natural History', 'Pacific Grove'),
    'PGM': ('Pacific Grove Museum of Natural History', 'Pacific Grove'),
    'SCC': ('San Carlos Cathedral', 'Monterey'),
    'ST': ('Sunset Center Theater', 'Carmel-by-the-Sea'),
    'ST/105': ('Sunset Center Studio 105', 'Carmel-by-the-Sea'),
    'ST/FOY': ('Sunset Center Foyer', 'Carmel-by-the-Sea'),
    'ST/TERRACE': ('Sunset Center Terrace', 'Carmel-by-the-Sea'),
}

# These schedule entries are explicitly non-performance ancillary events.
EXCLUDED_TITLES = {
    'closing night dinner',
    'movie night',
}

COLUMNS = [
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
]


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized_title(value):
    value = clean_text(value).lower().replace('&', ' and ')
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def get_pages(session):
    response = session.get(
        PAGES_API,
        params={
            'per_page': 100,
            '_fields': 'id,slug,link,title,content',
        },
        timeout=45,
    )
    response.raise_for_status()
    pages = response.json()
    total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
    for page_number in range(2, total_pages + 1):
        next_response = session.get(
            PAGES_API,
            params={
                'per_page': 100,
                'page': page_number,
                '_fields': 'id,slug,link,title,content',
            },
            timeout=45,
        )
        next_response.raise_for_status()
        pages.extend(next_response.json())
    return pages


def detail_index(pages):
    index = {}
    for page in pages:
        title = clean_text((page.get('title') or {}).get('rendered'))
        content = clean_text((page.get('content') or {}).get('rendered'))
        if title and content:
            index[normalized_title(title)] = (page.get('link'), content)
    return index


def find_detail(title, index):
    key = normalized_title(title)
    if key in index:
        return index[key]

    # Detail-page titles sometimes omit punctuation or add a small heading.
    candidates = [
        value for candidate, value in index.items()
        if len(key) >= 8 and (key in candidate or candidate in key)
    ]
    return candidates[0] if len(candidates) == 1 else (None, None)


def schedule_year(text):
    match = re.search(r'\b(20\d{2})\s+At-A-Glance\b', text, re.IGNORECASE)
    if not match:
        match = re.search(r'\b(20\d{2})\b', text)
    return int(match.group(1)) if match else None


def parse_schedule(schedule_page, pages):
    content = (schedule_page.get('content') or {}).get('rendered') or ''
    lines = [
        re.sub(r'\s+', ' ', line).strip()
        for line in BeautifulSoup(content, 'html.parser').get_text('\n').splitlines()
        if line.strip()
    ]
    text = '\n'.join(lines)
    year = schedule_year(text)
    if year is None:
        raise ValueError('Could not determine the schedule year')

    details = detail_index(pages)
    current_date = None
    records = []
    started = False
    for line in lines:
        if line == 'Pre-Fest':
            started = True
            continue
        if not started:
            continue
        if line == 'Explore 3 Day Passes':
            break

        day_match = re.fullmatch(
            r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), '
            r'([A-Z][a-z]+) (\d{1,2})',
            line,
        )
        if day_match:
            try:
                current_date = datetime.strptime(
                    f'{day_match.group(1)} {day_match.group(2)} {year}', '%B %d %Y'
                ).date().isoformat()
            except ValueError:
                current_date = None
            continue

        event_match = re.fullmatch(
            r'(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*\|\s*(.+?)\s*\(([^()]+)\)\*?',
            line,
            re.IGNORECASE,
        )
        if not event_match or not current_date:
            continue

        title = event_match.group(2).strip().rstrip('*').strip()
        if normalized_title(title) in EXCLUDED_TITLES:
            continue
        venue_code = event_match.group(3).strip().upper()
        location = VENUES.get(venue_code)
        if not title or not location:
            continue

        time_from = datetime.strptime(
            re.sub(r'\s+', '', event_match.group(1)).upper(),
            '%I:%M%p' if ':' in event_match.group(1) else '%I%p',
        ).strftime('%H:%M')
        detail_url, description = find_detail(title, details)
        venue, city = location
        records.append({
            'title': title,
            'date': date.fromisoformat(current_date).isoformat(),
            'url': detail_url or schedule_page['link'],
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


class BachfestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachfestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=COLUMNS,
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            pages = get_pages(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Carmel Bach Festival pages',
                event='crawler_fetch_failed',
                level='error',
                url=PAGES_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        schedule_page = next(
            (page for page in pages if page.get('slug') == SCHEDULE_SLUG), None
        )
        if schedule_page is None:
            raise ValueError('Could not find the Carmel Bach Festival schedule page')
        return sorted(
            parse_schedule(schedule_page, pages),
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    BachfestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
