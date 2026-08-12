import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://amicimusicafoligno.it/home/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
SOURCE = 'Amici della Musica Foligno ETS'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

DATE_LINE_RE = re.compile(
    r'^(?:lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|giovedi|giovedì|'
    r'venerdi|venerdì|sabato|domenica)?\s*'
    r'(\d{1,2})(?:[°º])?\s+'
    r'(' + '|'.join(MONTHS) + r')\b'
    r'(?:\s+(?:ore|h)\s*(\d{1,2})(?:[.:](\d{2}))?)?'
    r'\s*(.*)$',
    re.IGNORECASE,
)

VENUE_RE = re.compile(
    r'\b(?:auditorium|oratorio|teatro|chiesa|corte|palazzo|villa|ospedale|'
    r'aula|fonti|chiostro|cortile|castello)\b',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def season_category(rendered):
    text = html.unescape(rendered or '')
    match = re.search(r'\bcategories:(\d+)\b', text)
    return int(match.group(1)) if match else None


def get_json(session, endpoint, params):
    url = f'{API_URL}/{endpoint}'
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json(), response.headers


def season_categories(session):
    categories = []
    for year in range(2018, date.today().year + 2):
        pages, _ = get_json(
            session,
            'pages',
            {'slug': f'stagione-{year}', '_fields': 'content'},
        )
        if not pages:
            continue
        category = season_category(pages[0].get('content', {}).get('rendered'))
        if category is not None:
            categories.append((year, category))
    return categories


def category_posts(session, category):
    posts = []
    page = 1
    while True:
        batch, headers = get_json(
            session,
            'posts',
            {
                'categories': category,
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,content',
            },
        )
        posts.extend(batch)
        total_pages = int(headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return posts
        page += 1


def extract_venue_and_city(location):
    location = clean_text(location).strip(' ,;-')
    if not location:
        return None

    city = 'Foligno'
    explicit_city = re.match(r'^([^,–—]+)\s*[,–—]\s*(.+)$', location)
    if explicit_city:
        possible_city, venue = explicit_city.groups()
        if possible_city.casefold() not in {
            'auditorium', 'teatro', 'chiesa', 'oratorio', 'corte', 'palazzo',
        }:
            city = possible_city.strip()
            location = venue.strip()

    if not location or location.casefold() == city.casefold():
        return None
    return location, city


def parse_post(post, year):
    rendered = post.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(rendered, 'html.parser')
    text = clean_text(soup)
    title = clean_text(BeautifulSoup(post.get('title', {}).get('rendered', ''), 'html.parser'))
    url = post.get('link')
    if not title or not url or not text:
        return []

    lines = [line for line in text.splitlines() if line]
    occurrences = []
    for index, line in enumerate(lines):
        match = DATE_LINE_RE.match(line)
        if not match:
            continue
        day_text, month_text, hour_text, minute_text, location = match.groups()
        try:
            event_date = date(year, MONTHS[month_text.casefold()], int(day_text)).isoformat()
        except (KeyError, ValueError):
            continue

        alternate_time = re.match(
            r'^[–—-]?\s*(?:ore|h)\s*(\d{1,2})(?:[.:](\d{2}))?\s*(.*)$',
            location,
            re.IGNORECASE,
        )
        if alternate_time:
            hour_text, minute_text, location = alternate_time.groups()

        # Layouts vary by season. Find a clearly labelled venue near the date;
        # never turn an intervening heading or prose sentence into a venue.
        candidates = [location]
        following = index + 1
        while following < len(lines) and len(candidates) < 6:
            if not DATE_LINE_RE.match(lines[following]):
                candidates.append(lines[following])
            following += 1
        location = ''
        for candidate in candidates:
            candidate_time = re.match(
                r'^[–—-]?\s*(?:ore|h)\s*(\d{1,2})(?:[.:](\d{2}))?\s*(.*)$',
                candidate,
                re.IGNORECASE,
            )
            if candidate_time:
                if hour_text is None:
                    hour_text, minute_text = candidate_time.group(1, 2)
                candidate = candidate_time.group(3)
            candidate = candidate.strip(' ,;–—-')
            if VENUE_RE.search(candidate):
                location = candidate
                break

        venue_city = extract_venue_and_city(location)
        if venue_city is None:
            continue
        venue, city = venue_city
        time_from = None
        if hour_text is not None:
            hour = int(hour_text)
            minute = int(minute_text or '00')
            if hour > 23 or minute > 59:
                continue
            time_from = f'{hour:02d}:{minute:02d}'

        occurrences.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': text,
        })
    return occurrences


class AmicimusicafolignoItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amicimusicafoligno_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        try:
            categories = season_categories(session)
            for year, category in categories:
                for post in category_posts(session, category):
                    records.extend(parse_post(post, year))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Amici della Musica Foligno API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    AmicimusicafolignoItCrawler().run()


if __name__ == '__main__':
    main()
