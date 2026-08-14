import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lithuanianchamberorchestra.com/'
SOURCE = 'Lithuanian Chamber Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
CONCERTS_CATEGORY_ID = 20
PAGE_SIZE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}

TOUR_LOCATIONS = {
    'La Palma': ('Santa Cruz de La Palma', 'Circus of Mars Theatre'),
    'Tenerife': ('Santa Cruz de Tenerife', 'Tenerife Auditorium'),
    'Lanzarotte': ('Arrecife', 'Víctor Fernández Gopar Theatre – El Salinero'),
    'Lanzarote': ('Arrecife', 'Víctor Fernández Gopar Theatre – El Salinero'),
    'Fuerteventura': ('Puerto del Rosario', 'Training and Conference Centre'),
    'Gran Canaria': ('Las Palmas de Gran Canaria', 'Alfredo Kraus Auditorium'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\ufffc', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def valid_date(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return None


def record(title, event_date, url, time_from, venue, city, country_code, description):
    if not all((title, event_date, url, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_vilnius_concert(post, title, text):
    date_match = re.search(
        r'(?P<year>20\d{2})\s+(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})'
        r'(?:,\s*[A-Za-z]+)?,?\s+(?P<hour>\d{1,2})[.:](?P<minute>\d{2})',
        text,
        re.IGNORECASE,
    )
    venue_match = re.search(
        r'(Lithuanian National Philharmonic Concert Hall)\s*,\s*(Vilnius)',
        text,
        re.IGNORECASE,
    )
    if not date_match or not venue_match:
        return []
    month = MONTHS.get(date_match.group('month').lower())
    event_date = valid_date(date_match.group('year'), month, date_match.group('day'))
    item = record(
        title,
        event_date,
        post.get('link'),
        f"{int(date_match.group('hour')):02d}:{date_match.group('minute')}",
        venue_match.group(1),
        venue_match.group(2),
        'LT',
        text,
    )
    return [item] if item else []


def parse_canary_tour(post, title, text):
    if 'FESTIVAL INTERNACIONAL DE MUSICA DE CANARIAS' not in title.upper():
        return []
    try:
        year = int(post['date'][:4])
    except (KeyError, TypeError, ValueError):
        return []

    records = []
    for island, (city, venue) in TOUR_LOCATIONS.items():
        match = re.search(
            rf'{re.escape(island)}\s*[–—-]\s*{re.escape(venue)}\s*,\s*(\d{{1,2}})\s+January',
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        event_date = valid_date(year, 1, match.group(1))
        item = record(
            title,
            event_date,
            post.get('link'),
            None,
            venue,
            city,
            'ES',
            text,
        )
        if item:
            records.append(item)
    return records


def parse_post(post):
    title = clean_text((post.get('title') or {}).get('rendered'))
    text = clean_text((post.get('content') or {}).get('rendered'))
    if not title or not text or not post.get('link'):
        return []
    records = parse_canary_tour(post, title, text)
    if records:
        return records
    return parse_vilnius_concert(post, title, text)


def get_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'categories': CONCERTS_CATEGORY_ID,
                'per_page': PAGE_SIZE,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
            },
            timeout=60,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError('WordPress posts response is not a list')
        posts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return posts
        page += 1


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        posts = get_posts(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to scrape concert feed',
            event='crawler_feed_failed',
            level='warning',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    records = [item for post in posts for item in parse_post(post)]
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['venue'], item['title']
        ),
    )


class LithuanianChamberOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lithuanianchamberorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LT',
        upload_target='potential',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LithuanianChamberOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
