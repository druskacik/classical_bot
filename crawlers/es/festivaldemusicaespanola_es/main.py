import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivaldemusicaespanola.es/'
SOURCE = 'Festival de Música Española de León'
DEFAULT_CITY = 'León'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}
MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}
DATE_RE = re.compile(
    r'^(?:lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo),?\s*'
    r'(\d{1,2})\s+de\s+([a-záéíóúñ]+)(?:\s+de\s+(20\d{2}))?'
    r'(?:\s*\|\s*([0-2]?\d:[0-5]\d)\s*h?)?$',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'^([0-2]?\d:[0-5]\d)\s*h?$', re.IGNORECASE)
NON_VENUE_PREFIXES = (
    'entrada', 'programa', 'notas ', 'intérpretes', 'interpretes',
    'venta ', 'precio', 'aforo', 'duración', 'duracion',
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def content_lines(soup):
    main = soup.select_one('main') or soup
    lines = [clean_text(line) for line in main.get_text('\n').splitlines()]
    return [line for line in lines if line and line not in ('top of page', 'bottom of page')]


def discover_programme(session):
    soup = get_soup(session, SOURCE_URL)
    candidates = []
    for link in soup.select('a[href]'):
        href = urljoin(SOURCE_URL, link.get('href'))
        match = re.search(r'/programacion-(20\d{2})/?$', urlparse(href).path)
        if match:
            candidates.append((int(match.group(1)), href))
    if not candidates:
        raise ValueError('Could not find a yearly programme page')
    return max(candidates)


def discover_detail_urls(soup):
    urls = set()
    for link in soup.select('main a[href]'):
        href = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(href)
        if parsed.netloc not in ('festivaldemusicaespanola.es', 'www.festivaldemusicaespanola.es'):
            continue
        if not link.find('img') or parsed.path in ('', '/'):
            continue
        urls.add(href.split('#', 1)[0])
    return sorted(urls)


def parse_date_line(line, default_year):
    match = DATE_RE.match(line)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        value = date(int(match.group(3) or default_year), month, int(match.group(1)))
    except ValueError:
        return None
    return value.isoformat(), match.group(4)


def plausible_location(line):
    lower = line.lower()
    if (not line or DATE_RE.match(line) or TIME_RE.match(line)
            or lower.startswith(NON_VENUE_PREFIXES) or line == '****'):
        return None
    if ':' in line:
        city, venue = (part.strip(' *') for part in line.split(':', 1))
        if city and venue and len(city) <= 60:
            return venue, city
    venue_words = ('auditorio', 'teatro', 'centro ', 'casa de', 'iglesia', 'museo',
                   'conservatorio', 'palacio', 'catedral', 'sala ')
    if any(word in lower for word in venue_words):
        return line.strip(' *'), DEFAULT_CITY
    return None


def location_at(lines, index):
    line = lines[index]
    if line.endswith(':') and index + 1 < len(lines):
        city = line[:-1].strip(' *')
        following = plausible_location(lines[index + 1])
        if city and following:
            return following[0], city
    if index > 0 and lines[index - 1].endswith(':'):
        city = lines[index - 1][:-1].strip(' *')
        location = plausible_location(line)
        if city and location:
            return location[0], city
    location = plausible_location(line)
    if location and index + 1 < len(lines) and lines[index + 1].lower() == 'de león':
        return f'{location[0]} de León', DEFAULT_CITY
    return location


def nearest_location(lines, index):
    for offset in (1, -1, 2, -2, 3, -3):
        candidate_index = index + offset
        if 0 <= candidate_index < len(lines):
            location = location_at(lines, candidate_index)
            if location:
                return location
    return None


def parse_detail(soup, url, year):
    lines = content_lines(soup)
    heading = soup.select_one('main h1') or soup.select_one('h1')
    title = clean_text(heading)
    if not title and lines:
        title = lines[0]
    if not title:
        return []

    description = '\n'.join(lines) or None
    records = []
    for index, line in enumerate(lines):
        parsed_date = parse_date_line(line, year)
        if not parsed_date:
            continue
        event_date, embedded_time = parsed_date
        time_from = embedded_time
        if not time_from and index + 1 < len(lines):
            time_match = TIME_RE.match(lines[index + 1])
            if time_match:
                time_from = time_match.group(1)
        location = nearest_location(lines, index)
        if not location:
            continue
        venue, city = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'ES',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class FestivalDeMusicaEspanolaEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivaldemusicaespanola_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            year, programme_url = discover_programme(session)
            programme_soup = get_soup(session, programme_url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Festival de Música Española programme',
                event='crawler_fetch_failed', level='error', url=SOURCE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        for url in discover_detail_urls(programme_soup):
            try:
                records.extend(parse_detail(get_soup(session, url), url, year))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Festival de Música Española concert',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
        unique = {
            (record['url'], record['date'], record['time_from'], record['venue']): record
            for record in records
        }
        return sorted(
            unique.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    FestivalDeMusicaEspanolaEsCrawler().run()


if __name__ == '__main__':
    main()
