import html
import re

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://jpo.co.za/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'Johannesburg Philharmonic Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-ZA,en;q=0.9',
}

VENUE_CITIES = {
    'linder auditorium': 'Johannesburg',
    'the linder auditorium': 'Johannesburg',
    'nirox sculpture park': 'Krugersdorp',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_posts(session):
    """Yield every published post, including the site's concert archive."""
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, '_fields': 'title,link,content'},
            timeout=60,
        )
        response.raise_for_status()
        posts = response.json()
        yield from posts
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1


def labelled_values(text, label):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    values = []
    for index, line in enumerate(lines):
        match = re.fullmatch(rf'{label}\s*:?\s*(.*)', line, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).lstrip(':').strip()
        cursor = index + 1
        while not value and cursor < len(lines) and lines[cursor] == ':':
            cursor += 1
        if not value and cursor < len(lines):
            value = lines[cursor].lstrip(':').strip()
        if value:
            values.append(value)
    return values


def parse_date(value):
    value = re.sub(r'(?<=\d)(st|nd|rd|th)\b', '', value, flags=re.IGNORECASE)
    value = re.sub(r'\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+', '', value, flags=re.I)
    # A range is a season overview rather than one concrete occurrence.
    if re.search(r'\d\s*(?:-|–|—|to)\s*\d', value, flags=re.I):
        return None
    try:
        parsed = date_parser.parse(value, dayfirst=True, fuzzy=False)
    except (ValueError, OverflowError):
        return None
    return parsed.date().isoformat()


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*(?:h|:|\.)([0-5]\d)\b', value, flags=re.I)
    if match:
        return f'{int(match.group(1)):02d}:{match.group(2)}'
    match = re.search(r'\b(1[0-2]|0?[1-9])\s*(am|pm)\b', value, flags=re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(2).lower() == 'pm' else 0)
    return f'{hour:02d}:00'


def parse_post(post):
    content = clean_text(post.get('content', {}).get('rendered'))
    dates = [parsed for raw in labelled_values(content, 'DATE') if (parsed := parse_date(raw))]
    venues = labelled_values(content, 'VENUE')
    if not dates or not venues:
        return []

    venue = venues[0].strip(' :-')
    city = VENUE_CITIES.get(venue.casefold())
    if not venue or not city:
        return []

    title = clean_text(post.get('title', {}).get('rendered'))
    url = post.get('link', '').strip()
    if not title or not url:
        return []

    times = [parse_time(raw) for raw in labelled_values(content, 'TIME')]
    records = []
    for index, date in enumerate(dates):
        records.append(
            {
                'title': title,
                'date': date,
                'url': url,
                'time_from': times[index] if index < len(times) else None,
                'venue': venue,
                'city': city,
                'country_code': 'ZA',
                'description': content or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


class JpoCoZaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jpo_co_za',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ZA',
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
        records = []
        try:
            for post in get_posts(session):
                records.extend(parse_post(post))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch JPO concert archive',
                event='crawler_scrape_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    JpoCoZaCrawler().run()


if __name__ == '__main__':
    main()
