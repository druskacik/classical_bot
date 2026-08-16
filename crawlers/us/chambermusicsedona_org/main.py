import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chambermusicsedona.org/'
SOURCE = 'Chamber Music Sedona'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTHS = {
    'jan': 1,
    'january': 1,
    'feb': 2,
    'february': 2,
    'mar': 3,
    'march': 3,
    'apr': 4,
    'april': 4,
    'may': 5,
    'jun': 6,
    'june': 6,
    'jul': 7,
    'july': 7,
    'aug': 8,
    'august': 8,
    'sep': 9,
    'sept': 9,
    'september': 9,
    'oct': 10,
    'october': 10,
    'nov': 11,
    'november': 11,
    'dec': 12,
    'december': 12,
}

DATE_RE = re.compile(
    r'\b('
    + '|'.join(sorted(MONTHS, key=len, reverse=True))
    + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?(?=\s|\b)',
    re.IGNORECASE,
)

# These are the first-party venue names used near the event heading on concert
# pages.  Matching only the beginning of the page avoids artist-biography venue
# mentions being mistaken for the event location.
VENUES = (
    (re.compile(r'\bSedona Performing Arts Center\b', re.IGNORECASE), 'Sedona Performing Arts Center'),
    (re.compile(r'\bSedona Hilton\b', re.IGNORECASE), 'Sedona Hilton'),
    (re.compile(r'\bHilton Hotel\s*[–-]\s*Main Ballroom\b', re.IGNORECASE), 'Hilton Hotel – Main Ballroom'),
    (re.compile(r'\bThe Collective Sedona\b', re.IGNORECASE), 'The Collective Sedona'),
    (re.compile(r'\bCreative Life Center\b', re.IGNORECASE), 'Creative Life Center'),
    (re.compile(r'\b(?:held |in )?(?:at |in )?(?:a )?private (?:home|residence)\b', re.IGNORECASE), 'Private home'),
)

EXCLUDED_SLUG_PARTS = (
    'recent-concerts-',
    'reception',
    '-nyt',
)


def clean_text(rendered_html):
    soup = BeautifulSoup(rendered_html or '', 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(match):
    month = MONTHS[match.group(1).lower().rstrip('.')]
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_time(match):
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def clean_title(value):
    title = BeautifulSoup(html.unescape(value or ''), 'html.parser').get_text(' ', strip=True)
    title = re.sub(r'^\s*20\d{2}\s*[-–:]?\s*', '', title)
    title = re.sub(r'^Chamber Music Sedona Presents\s*', '', title, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', title).strip(' -–')


def parse_page(page):
    slug = page.get('slug', '')
    if slug == 'home' or any(part in slug for part in EXCLUDED_SLUG_PARTS):
        return []

    description = clean_text(page.get('content', {}).get('rendered'))
    # Concrete event metadata is consistently placed at the head of concert
    # pages. Limiting this window prevents dates and venues in biographies,
    # season summaries, and organization history from creating false events.
    event_header = description[:2200]
    date_match = DATE_RE.search(event_header)
    title = clean_title(page.get('title', {}).get('rendered'))
    if not title or not date_match:
        return []

    event_date = parse_date(date_match)
    if not event_date:
        return []

    venue = None
    venue_position = len(event_header)
    for pattern, name in VENUES:
        match = pattern.search(event_header)
        if match and match.start() < venue_position:
            venue = name
            venue_position = match.start()
    if not venue:
        return []

    if 'salon' in title.lower():
        private_home = VENUES[-1][0].search(event_header[:1200])
        if private_home:
            venue = 'Private home'

    time_window = event_header[max(0, date_match.start() - 180):date_match.end() + 120]
    times = []
    for match in TIME_RE.finditer(time_window):
        parsed = parse_time(match)
        if parsed not in times:
            times.append(parsed)
    if not times:
        times = [None]

    return [
        {
            'title': title,
            'date': event_date,
            'url': page['link'],
            'time_from': time_from,
            'venue': venue,
            'city': 'Sedona',
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in times
    ]


class ChamberMusicSedonaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicsedona_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        pages = []
        page_number = 1

        try:
            while True:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page_number,
                        'orderby': 'id',
                        'order': 'asc',
                        '_fields': 'id,slug,link,title,content',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                batch = response.json()
                pages.extend(batch)
                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
                if page_number >= total_pages:
                    break
                page_number += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Chamber Music Sedona pages',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for page in pages:
            records.extend(parse_page(page))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ChamberMusicSedonaOrgCrawler().run()


if __name__ == '__main__':
    main()
