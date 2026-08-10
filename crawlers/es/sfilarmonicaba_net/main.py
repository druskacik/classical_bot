import json
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sfilarmonicaba.net/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programacion/')
SOURCE = 'Sociedad Filarmónica de Badajoz'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
    'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9,
    'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}

# The organisation performs around Extremadura, so these are deliberately
# event-specific mappings rather than a blanket Badajoz default.
VENUES = [
    (r'teatro l[oó]pez de ayala', 'Teatro López de Ayala', 'Badajoz'),
    (r'palacio de congresos de badajoz', 'Palacio de Congresos de Badajoz', 'Badajoz'),
    (r'diputaci[oó]n (?:provincial )?de badajoz', 'Diputación de Badajoz', 'Badajoz'),
    (r'conservatorio superior de m[uú]sica bonifacio gil',
     'Conservatorio Superior de Música Bonifacio Gil', 'Badajoz'),
    (r'museo de bellas artes de badajoz', 'Museo de Bellas Artes de Badajoz', 'Badajoz'),
    (r'catedral (?:metropolitana )?de (?:san juan bautista de )?badajoz',
     'Catedral de Badajoz', 'Badajoz'),
    (r'iglesia de san andr[eé]s', 'Iglesia de San Andrés', 'Badajoz'),
    (r'convento de la merced de llerena', 'Convento de la Merced', 'Llerena'),
    (r'palacio de congresos de m[eé]rida', 'Palacio de Congresos de Mérida', 'Mérida'),
    (r'teatro carolina coronado', 'Teatro Carolina Coronado', 'Almendralejo'),
]

DATE_RE = re.compile(
    r'\b(?:el\s+)?([0-3]?\d)\s+de\s+(' + '|'.join(MONTHS) +
    r')(?:\s+de\s+(20\d{2}))?\b', re.I,
)
TIME_RE = re.compile(r'\b(?:a\s+las\s+)?([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:h(?:oras?)?\.?)?', re.I)


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def archive_urls(session):
    page = 1
    seen = set()
    while True:
        url = PROGRAM_URL if page == 1 else urljoin(PROGRAM_URL, f'page/{page}/')
        soup = get_soup(session, url)
        links = []
        for article in soup.select('article.type-post'):
            heading = article.select_one('.entry-title a[href], h2 a[href], h1 a[href]')
            if heading and heading['href'] not in seen:
                seen.add(heading['href'])
                links.append(heading['href'])
        if not links:
            break
        yield from links
        next_link = soup.select_one('a.pagination-next, .pagination a.next, a:has(span.meta-nav)')
        if not next_link and not any(
            urljoin(url, a.get('href', '')) == urljoin(PROGRAM_URL, f'page/{page + 1}/')
            for a in soup.select('a[href]')
        ):
            break
        page += 1


def published_date(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        for item in payload.get('@graph', []) if isinstance(payload, dict) else []:
            value = item.get('datePublished') if isinstance(item, dict) else None
            if value:
                try:
                    return date.fromisoformat(value[:10])
                except ValueError:
                    pass
    return None


def event_date(title, text, publication):
    title_matches = list(DATE_RE.finditer(title))
    matches = title_matches or list(DATE_RE.finditer(text))
    if not matches or not publication:
        return None
    if not title_matches:
        distinct_dates = {(match.group(1), match.group(2).lower(), match.group(3)) for match in matches}
        if len(distinct_dates) > 1:
            return None
    # The first explicit concert date is normally in the lead paragraph. If
    # an announcement in late year advertises an early-year date, roll forward.
    match = matches[0]
    day, month_name, year = match.groups()
    month = MONTHS[month_name.lower()]
    year = int(year) if year else publication.year
    if not match.group(3) and publication.month >= 10 and month <= 3:
        year += 1
    try:
        return date(year, month, int(day)).isoformat()
    except ValueError:
        return None


def event_location(title, text):
    normalized = text.lower()
    title_normalized = title.lower()
    candidates = []
    for pattern, venue, city in VENUES:
        match = re.search(pattern, normalized, re.I)
        if match:
            candidates.append((match.start(), venue, city))
    cities_in_title = {
        city for _, _, city in VENUES if re.search(rf'\b{re.escape(city.lower())}\b', title_normalized)
    }
    if cities_in_title:
        candidates = [item for item in candidates if item[2] in cities_in_title]
    if candidates:
        _, venue, city = min(candidates)
        return venue, city
    return None, None


def make_record(url, soup):
    article = soup.select_one('article.type-post')
    content = article.select_one('.entry-content') if article else None
    heading = article.select_one('h1.entry-title') if article else None
    title = clean_text(heading)
    description = clean_text(content)
    if not title or not description:
        return None

    # Programme archives also contain workshops and general festival news.
    # Require concert language, a real event date, and a known event venue.
    if not re.search(r'\b(concierto|recital|ensemble|orquesta|cuarteto|piano|m[uú]sica)\b',
                     title + ' ' + description[:1200], re.I):
        return None
    event_day = event_date(title, description[:2500], published_date(soup))
    venue, city = event_location(title, description[:2500])
    if not event_day or not venue or not city:
        return None
    time_match = TIME_RE.search(description[:1800])
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    return {
        'title': title,
        'date': event_day,
        'url': url,
        'time_from': time_from,
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
    records = []
    for url in archive_urls(session):
        try:
            record = make_record(url, get_soup(session, url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed', level='warning', url=url,
                error_type=type(error).__name__, error_message=str(error),
            )
            continue
        if record:
            records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SfilarmonicabaNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfilarmonicaba_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SfilarmonicabaNetCrawler().run()


if __name__ == '__main__':
    main()
