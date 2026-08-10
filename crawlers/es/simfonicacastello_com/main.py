import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://simfonicacastello.com/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'Orquestra Simfònica de Castelló'
CONCERT_CATEGORY_ID = 39

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'ca-ES,es-ES;q=0.9,es;q=0.8',
}

DATE_PREFIX_RE = re.compile(
    r'^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4}|\d{2})\s*'
    r'(?:[\xb7.\-:|]\s*)?'
)
TIME_RE = re.compile(
    r'(?i)(?:a\s+(?:les|las)|horari\s*:|horario\s*:|,\s*)\s*'
    r'([01]?\d|2[0-3])(?:\s*[:.]\s*([0-5]\d)|\s*h\b)'
)


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    text = (
        BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
        if '<' in value
        else value
    )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API,
            params={
                'categories': CONCERT_CATEGORY_ID,
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,content',
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError('Concert API returned an unexpected payload')
        posts.extend(payload)
        total_pages = int(response.headers.get('X-WP-TotalPages') or 1)
        if page >= total_pages:
            return posts
        page += 1


def parse_title_date(raw_title):
    match = DATE_PREFIX_RE.match(raw_title)
    if not match:
        return None, ''
    day, month, year = (int(value) for value in match.groups())
    if year < 100:
        year += 2000
    try:
        event_date = date(year, month, day).isoformat()
    except ValueError:
        return None, ''
    title = raw_title[match.end():].strip(' \t\n\xb7.-:|')
    return event_date, title


def resolve_location(description):
    normalized = re.sub(r'\s+', ' ', description)
    candidates = []
    location_patterns = [
        (
            r'(?i)jardines?\s+del\s+castell\s+de\s+peny[i\xed]scola',
            'Jardins del Castell de Peníscola',
            'Peníscola',
        ),
        (
            r'(?i)teatre\s+municipal\s+de\s+benic[aà]ssim',
            'Teatre Municipal de Benicàssim',
            'Benicàssim',
        ),
        (
            r'(?i)pla[cç]a\s+de\s+les\s+aules\s+de\s+castell[oó]n?',
            'Plaça de les Aules',
            'Castelló de la Plana',
        ),
        (
            r'(?i)plaza\s+de\s+toros\s+de\s+castell[oó]n',
            'Plaza de Toros de Castellón',
            'Castelló de la Plana',
        ),
        (
            r'(?i)(?:sala\s+sinf[oó]nica\s+(?:del\s+)?)?auditori(?:o)?'
            r'(?:\s+i|\s+y)?\s*(?:palau|palacio)?\s*'
            r'(?:de\s+congressos|de\s+congresos)?\s+de\s+castell[oó]n?',
            'Auditori i Palau de Congressos de Castelló',
            'Castelló de la Plana',
        ),
    ]
    for pattern, venue, city in location_patterns:
        match = re.search(pattern, normalized)
        if match:
            if venue.startswith('Auditori') and re.search(
                r'(?i)sala\s+sinf[oó]nica', match.group(0)
            ):
                venue += ' – Sala Simfònica'
            candidates.append((match.start(), venue, city))

    if not candidates:
        return None, None
    _, venue, city = min(candidates)
    return venue, city


def parse_time(description):
    match = TIME_RE.search(re.sub(r'\s+', ' ', description))
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_post(post):
    title_data = post.get('title') or {}
    content_data = post.get('content') or {}
    raw_title = clean_text(title_data.get('rendered'))
    description = clean_text(content_data.get('rendered'))
    event_date, title = parse_title_date(raw_title)
    venue, city = resolve_location(description)
    url = clean_text(post.get('link'))

    if not title or not event_date or not url or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    posts = fetch_posts(session)
    records = [record for post in posts if (record := parse_post(post))]
    skipped_count = len(posts) - len(records)
    if skipped_count:
        log_message(
            'Skipped concert posts without a complete date and location',
            event='crawler_items_skipped',
            level='info',
            record_count=skipped_count,
            url=EVENTS_API,
        )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class SimfonicacastelloComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='simfonicacastello_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SimfonicacastelloComCrawler().run()


if __name__ == '__main__':
    main()
