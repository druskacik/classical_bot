import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orquestadeleonodonalonso.es/'
POSTS_API = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'Orquesta Sinfónica Ciudad de León «Odón Alonso»'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'es-ES,es;q=0.9',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'setiembre': 9, 'octubre': 10,
    'noviembre': 11, 'diciembre': 12,
}
DATE_RE = re.compile(
    r'\b(\d{1,2})\s+(?:de\s+)?('
    + '|'.join(MONTHS)
    + r')(?:\s+(?:de\s+)?(20\d{2}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(?:a\s+las?\s+)?([012]?\d)[:.]([0-5]\d)\s*(?:h(?:oras?)?)?\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_posts(session):
    response = session.get(POSTS_API, params={'per_page': 100}, timeout=60)
    response.raise_for_status()
    return response.json()


def event_date(text, published_year):
    match = DATE_RE.search(text[:3000])
    if not match:
        return None, None
    year = int(match.group(3) or published_year)
    try:
        value = date(year, MONTHS[match.group(2).lower()], int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    return value, match


def event_times(text, date_match):
    nearby = text[date_match.end():date_match.end() + 260]
    matches = TIME_RE.findall(nearby)
    times = []
    for hour, minute in matches:
        value = f'{int(hour):02d}:{minute}'
        if value not in times:
            times.append(value)
    return times or [None]


def location(text):
    lead = text[:3000]
    if re.search(r'Auditorio(?:\s+Ciudad)?\s+de\s+Oviedo', lead, re.I):
        return 'Auditorio de Oviedo', 'Oviedo'
    if re.search(r'Auditorio(?:\s+Ciudad)?\s+de\s+Le[oó]n', lead, re.I):
        return 'Auditorio Ciudad de León', 'León'
    # The orchestra is resident at León's municipal auditorium and older posts
    # refer to it simply as "el auditorio" or "Auditorio de la capital".
    if re.search(r'\b(?:el\s+|al\s+)?auditorio\b', lead, re.I):
        return 'Auditorio Ciudad de León', 'León'
    return None, None


def regular_records(post):
    title = clean_text((post.get('title') or {}).get('rendered'))
    content = clean_text((post.get('content') or {}).get('rendered'))
    combined = '\n'.join(part for part in (title, content) if part)
    published_year = int((post.get('date') or '')[:4])
    concert_date, match = event_date(combined, published_year)
    venue, city = location(combined)
    url = post.get('link') or ''

    if not title:
        title = clean_text(content.split('\n', 1)[0])
    if not title or not concert_date or not match or not venue or not city or not url:
        return []

    return [
        {
            'title': title,
            'date': concert_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': content or None,
        }
        for time_from in event_times(combined, match)
    ]


def touring_christmas_records(post):
    """The 2019 article announces three consecutive concerts in three cities."""
    content = clean_text((post.get('content') or {}).get('rendered'))
    title = clean_text((post.get('title') or {}).get('rendered'))
    url = post.get('link') or ''
    locations = (
        ('2019-12-21', 'Teatro Ramos Carrión', 'Zamora'),
        ('2019-12-22', 'Auditorio Ciudad de León', 'León'),
        ('2019-12-23', 'Parroquia de San Pedro Apóstol', 'Valencia de Don Juan'),
    )
    return [
        {
            'title': title,
            'date': concert_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'description': content or None,
        }
        for concert_date, venue, city in locations
    ]


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    posts = fetch_posts(session)
    records = []

    for post in posts:
        # Categories 2 and 5 are the site's current and historical concert
        # categories. Other categories also contain interviews and news.
        if not set(post.get('categories') or []) & {2, 5}:
            continue
        try:
            if post.get('id') == 209:
                records.extend(touring_christmas_records(post))
            else:
                records.extend(regular_records(post))
        except (TypeError, ValueError) as error:
            log_message(
                'Failed to parse Orquesta Odón Alonso concert post',
                event='crawler_item_failed',
                level='warning',
                url=post.get('link'),
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['city'], item['title']),
    )


class OrquestaDeLeonOdonAlonsoEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestadeleonodonalonso_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    OrquestaDeLeonOdonAlonsoEsCrawler().run()


if __name__ == '__main__':
    main()
