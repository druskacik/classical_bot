import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orquestafilarmonica.montevideo.gub.uy/'
CALENDAR_URL = urljoin(SOURCE_URL, 'eventos/mes/')
SOURCE = 'Orquesta Filarmónica de Montevideo'
CITY = 'Montevideo'
ARCHIVE_START_YEAR = 2023

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-UY,es;q=0.9',
}

VENUE_WORDS = (
    'teatro',
    'sala',
    'auditorio',
    'iglesia',
    'catedral',
    'basílica',
    'templo',
    'escuela',
    'liceo',
    'universidad',
    'centro cultural',
    'espacio cultural',
    'museo',
    'club ',
    'complejo ',
    'estadio',
    'plaza ',
    'parque ',
    'mercado ',
    'cabildo',
    'antel arena',
    'sodre',
    'rural del prado',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_months():
    # The Drupal calendar was introduced in 2023. Scan its complete archive
    # and a year ahead so dates announced for the following season are found.
    final_year = date.today().year + 1
    return [
        f'{year}-{month:02d}'
        for year in range(ARCHIVE_START_YEAR, final_year + 1)
        for month in range(1, 13)
    ]


def listing_occurrences(session, month):
    url = f'{CALENDAR_URL}{month}'
    try:
        soup = get_soup(session, url)
    except requests.RequestException as error:
        log_message(
            'Failed to scrape calendar month',
            event='crawler_page_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    occurrences = []
    current_date = None
    content = soup.select_one('.view-content')
    if not content:
        return occurrences

    for element in content.find_all(['h3', 'article'], recursive=True):
        if element.name == 'h3':
            stamp = element.select_one('[property="dc:date"][content]')
            raw_date = (stamp.get('content') if stamp else '')[:10]
            try:
                current_date = date.fromisoformat(raw_date).isoformat()
            except ValueError:
                current_date = None
            continue
        if 'node-evento' not in (element.get('class') or []) or not current_date:
            continue
        link = element.select_one('h2.node-title a[href], h2.node__title a[href]')
        title = clean_text(link.get_text(' ', strip=True)) if link else ''
        event_url = urljoin(SOURCE_URL, link.get('href')) if link else ''
        if title and event_url:
            occurrences.append({'title': title, 'date': current_date, 'url': event_url})
    return occurrences


def extract_time(text):
    # Event pages commonly use Spanish forms such as 19.30 h, 20h or 20:30hs.
    matches = re.findall(r'(?<!\d)([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)\s*(?:h(?:s|oras?)?\b)?', text, re.I)
    if matches:
        hour, minute = matches[0]
        return f'{int(hour):02d}:{minute}'
    matches = re.findall(r'(?<!\d)([01]?\d|2[0-3])\s*h(?:s|oras?)?\b', text, re.I)
    if matches:
        return f'{int(matches[0]):02d}:00'
    return None


MONTH_NAMES = (
    'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
    'julio', 'agosto', 'setiembre', 'octubre', 'noviembre', 'diciembre',
)


def extract_venue(text, event_date):
    parsed_date = date.fromisoformat(event_date)
    month_names = {MONTH_NAMES[parsed_date.month - 1]}
    if parsed_date.month == 9:
        month_names.add('septiembre')
    dated_candidates = []
    candidates = []
    venue_unconfirmed_for_date = False
    for line in (part.strip(' -–—,.;') for part in text.splitlines()):
        lowered = line.lower()
        if not line or len(line) > 120:
            continue
        has_day = re.search(rf'(?<!\d){parsed_date.day}(?!\d)', lowered)
        matches_date = has_day and any(month in lowered for month in month_names)
        if matches_date and re.search(r'locaci[oó]n\s+a\s+confirmar|lugar\s+a\s+confirmar', lowered):
            venue_unconfirmed_for_date = True
        positions = [lowered.find(word) for word in VENUE_WORDS if word in lowered]
        if positions and not any(term in lowered for term in ('entrada', 'boletería', 'duración')):
            venue = line[min(positions):]
            venue = re.sub(
                r'\s*[,;|–—-]?\s*(?:a\s+las\s+)?(?:[01]?\d|2[0-3])'
                r'(?:\s*[:.]\s*[0-5]\d)?\s*h(?:s|oras?)?\b.*$',
                '',
                venue,
                flags=re.I,
            ).strip(' -–—,.;')
            if not venue:
                continue
            candidates.append(venue)
            if matches_date:
                dated_candidates.append(venue)
    if venue_unconfirmed_for_date and not dated_candidates:
        return None
    if dated_candidates:
        return dated_candidates[0]
    if candidates:
        return candidates[0]
    return None


def detail_record(session, occurrence):
    try:
        soup = get_soup(session, occurrence['url'])
    except requests.RequestException as error:
        log_message(
            'Failed to scrape concert detail',
            event='crawler_item_failed',
            level='warning',
            url=occurrence['url'],
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    article = soup.select_one('article.node-evento')
    body = article.select_one('.field-name-body') if article else None
    description = clean_text(body) or None
    venue = extract_venue(description or '', occurrence['date'])
    if not venue:
        return None

    return {
        **occurrence,
        'time_from': extract_time(description or ''),
        'venue': venue,
        'city': CITY,
        'description': description,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    occurrences = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(listing_occurrences, session, month) for month in calendar_months()]
        for future in as_completed(futures):
            occurrences.extend(future.result())

    unique = {
        (item['url'], item['date']): item
        for item in occurrences
    }
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(detail_record, session, item) for item in unique.values()]
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class OrquestaFilarmonicaMontevideoGubUyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestafilarmonica_montevideo_gub_uy',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='UY',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OrquestaFilarmonicaMontevideoGubUyCrawler().run()


if __name__ == '__main__':
    main()
