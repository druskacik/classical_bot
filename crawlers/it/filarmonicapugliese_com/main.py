import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filarmonicapugliese.com/'
SOURCE = 'Orchestra Filarmonica Pugliese'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
EVENT_CATEGORY_ID = 6

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gen': 1, 'gennaio': 1, 'feb': 2, 'febbraio': 2,
    'mar': 3, 'marzo': 3, 'apr': 4, 'aprile': 4,
    'mag': 5, 'maggio': 5, 'giu': 6, 'giugno': 6,
    'lug': 7, 'luglio': 7, 'ago': 8, 'agosto': 8,
    'set': 9, 'sett': 9, 'settembre': 9, 'ott': 10, 'ottobre': 10,
    'nov': 11, 'novembre': 11, 'dic': 12, 'dicembre': 12,
}

# The archive has used both "City, Venue" and "Venue, City" title formats.
# These are cities explicitly observed in the source's event archive, not
# inferred from the orchestra's home location (the orchestra tours widely).
CITIES = {
    'acquaviva delle fonti', 'andria', 'bari', 'bari santo spirito',
    'bisceglie', 'brindisi', 'castellana grotte', 'corato', 'foggia',
    'giovinazzo', 'gravina di puglia', 'lecce', 'molfetta',
    'montegiordano marina', 'ostuni', 'roma', 'trani', 'vieste',
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(title, published):
    match = re.search(
        r'(?<!\d)(\d{1,2})\s+(gen(?:naio)?|feb(?:braio)?|mar(?:zo)?|apr(?:ile)?|'
        r'mag(?:gio)?|giu(?:gno)?|lug(?:lio)?|ago(?:sto)?|set(?:t(?:embre)?)?|'
        r'ott(?:obre)?|nov(?:embre)?|dic(?:embre)?)(?:\s+(\d{4}))?',
        title,
        re.IGNORECASE,
    )
    if not match:
        return None
    year = int(match.group(3)) if match.group(3) else int(published[:4])
    try:
        return date(year, MONTHS[match.group(2).casefold()], int(match.group(1))).isoformat()
    except (KeyError, ValueError):
        return None


def parse_time(title):
    match = re.search(r'\bore\s+([01]?\d|2[0-3])[.:]([0-5]\d)\b', title, re.IGNORECASE)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def normalize_location(value):
    return re.sub(r'^[\s\-–—,]+|[\s\-–—,]+$', '', value).strip()


def parse_location(title):
    """Parse only explicit two-part locations; never invent a tour venue."""
    segments = [normalize_location(part) for part in re.split(r'\s+[–—]\s+', title)]
    # A comma in an unsplit title is commonly just "event title, city" and
    # therefore does not establish a venue. Location pairs are accepted only
    # from a distinct dash-delimited title segment.
    candidates = list(reversed(segments[1:]))
    for candidate in candidates:
        if ',' not in candidate:
            continue
        parts = [normalize_location(part) for part in candidate.rsplit(',', 1)]
        if len(parts) != 2 or not all(parts):
            continue
        left, right = parts
        left_key = left.casefold()
        right_key = right.casefold()
        if left_key in CITIES and right_key not in CITIES:
            return right, left
        if right_key in CITIES and left_key not in CITIES:
            return left, right
    return None


def parse_description(rendered):
    # Remove Visual Composer shortcodes while preserving any authored prose or
    # programme text between them. Gallery image IDs are not useful descriptions.
    text = re.sub(r'\[(?:/?)[^\]]+\]', '\n', html.unescape(rendered or ''))
    text = clean_text(text)
    return text if text and text.casefold() != 'ee' else None


class FilarmonicaPuglieseComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonicapugliese_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        posts = []
        page = 1
        while True:
            params = {
                'categories': EVENT_CATEGORY_ID,
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
            }
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Orchestra Filarmonica Pugliese event archive',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page_number=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            page_posts = response.json()
            posts.extend(page_posts)
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        records = []
        for post in posts:
            title = clean_text(post.get('title', {}).get('rendered'))
            event_date = parse_date(title, post.get('date', ''))
            location = parse_location(title)
            url = post.get('link', '').strip()
            if not title or not event_date or not location or not url:
                continue
            venue, city = location
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(title),
                'venue': venue,
                'city': city,
                'country_code': 'IT',
                'description': parse_description(post.get('content', {}).get('rendered', '')),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FilarmonicaPuglieseComCrawler().run()


if __name__ == '__main__':
    main()
