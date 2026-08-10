import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orquestasinfonicadeburgos.com/'
SOURCE = 'Orquesta Sinfónica de Burgos'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
CITY = 'Burgos'
DEFAULT_VENUE = 'Fórum Evolución Burgos'

SEASON_SLUGS = {
    'temporadas',
    'temp-21-22',
    'temp-22-23',
    'temporada-2023-2024',
    'temporada2025-2026',
    'temporada-2023-2024-2',
}

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
MONTH_PATTERN = '|'.join(MONTHS)
DATE_PATTERN = rf'(\d{{1,2}})\s+(?:de\s+)?({MONTH_PATTERN})(?:\s+(?:de\s+){{0,2}}(20\d{{2}}))?'

# Concert headings are much more reliable than finding every date: the pages
# also contain rehearsal, ticket-sale and school-performance dates.
HEADING_RE = re.compile(
    r'(?i)(?:'
    r'(?:primer|segundo|tercer|cuarto|quinto|[1-5](?:º|er))\s+concierto'
    r'|concierto\s+familiar(?:\s+y\s+conciertos\s+pedag[oó]gicos)?'
    r'|concierto\s+extraordinario\s+fuera\s+de\s+temporada'
    r')\s*[:.]?\s*(?:año\s+(20\d{2})[.,]?\s*)?[^\d]{0,80}?'
    + DATE_PATTERN
)

# Seasons 2013-2017 list their events directly by weekday rather than giving
# each one an ordinal heading. These dates occur after the old-season marker.
OLD_DATE_RE = re.compile(
    rf'(?i)\b(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+'
    + DATE_PATTERN
)

TIME_RE = re.compile(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:h(?:oras?)?)?\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def fetch_pages(session):
    response = session.get(
        PAGES_API,
        params={'per_page': 100, '_fields': 'slug,link,content'},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def iso_date(day, month, year):
    try:
        return date(int(year), MONTHS[month.lower()], int(day)).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def venue_from_text(text):
    if re.search(r'teatro\s+principal(?:\s+de\s+burgos)?', text, re.I):
        return 'Teatro Principal de Burgos'
    if re.search(r'sala\s+de\s+congresos', text, re.I):
        return 'Sala de Congresos del Fórum Evolución Burgos'
    if re.search(r'(?:sala\s+)?auditorio(?:\s+rafael\s+fr[uü]hbeck\s+de\s+burgos)?', text, re.I):
        return 'Auditorio del Fórum Evolución Burgos'
    if re.search(r'f[oó]rum\s+evoluci[oó]n', text, re.I):
        return DEFAULT_VENUE
    if re.search(r'catedral\s+de\s+burgos', text, re.I):
        return 'Catedral de Burgos'
    return None


def event_title(block, heading):
    label = re.sub(r'\s+', ' ', heading).strip(' .:')
    # Preserve a short named programme when it immediately follows the date.
    after_date = re.split(DATE_PATTERN, block, maxsplit=1, flags=re.I)[-1]
    candidate = re.split(
        r'(?i)\b(?:auditorio|sala\s+de\s+congresos|teatro\s+principal|ensayos?|programa)\b',
        after_date,
        maxsplit=1,
    )[0].strip(' .:–-')
    if candidate and len(candidate) <= 90 and not TIME_RE.search(candidate):
        return f'{SOURCE} — {candidate}'
    return f'{SOURCE} — {label}'


def record_from_match(text, match, end, page_url, groups_offset=0):
    block = text[match.start():end].strip()
    year_hint, day, month, explicit_year = match.groups()[groups_offset:groups_offset + 4]
    concert_date = iso_date(day, month, explicit_year or year_hint)
    # These are the orchestra's own season pages. Since 2012 its stated home
    # for season concerts is Fórum Evolución; use that only where an archived
    # listing omits the room, while retaining every explicitly named venue.
    venue = venue_from_text(block[:500]) or DEFAULT_VENUE
    if not concert_date or not venue:
        return None

    date_end = match.end() - match.start()
    time_match = TIME_RE.search(block, date_end, min(len(block), date_end + 220))
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    heading = block[:match.end() - match.start()]
    return {
        'title': event_title(block, heading),
        'date': concert_date,
        'url': page_url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'description': block,
    }


def parse_standard_page(text, page_url):
    matches = list(HEADING_RE.finditer(text))
    records = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        record = record_from_match(text, match, end, page_url)
        if record:
            records.append(record)
    return records


def parse_old_seasons(text, page_url):
    # Only the 2013-2017 portion uses bare weekday headings. Stop before the
    # newer seasons, which are already handled by the ordinal-heading parser.
    start = text.lower().find('temporada 2017')
    if start < 0:
        return []
    old_text = text[start:]
    matches = list(OLD_DATE_RE.finditer(old_text))
    records = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(old_text)
        block = old_text[match.start():end].strip()
        day, month, year = match.groups()
        concert_date = iso_date(day, month, year)
        venue = venue_from_text(block[:400]) or DEFAULT_VENUE
        if not concert_date:
            continue
        time_match = TIME_RE.search(block, match.end() - match.start(), min(len(block), 180))
        records.append({
            'title': f'{SOURCE} — concierto del {concert_date}',
            'date': concert_date,
            'url': page_url,
            'time_from': (
                f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
            ),
            'venue': venue,
            'city': CITY,
            'description': block,
        })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []

    for page in fetch_pages(session):
        if page.get('slug') not in SEASON_SLUGS:
            continue
        try:
            text = clean_text((page.get('content') or {}).get('rendered'))
            page_url = page.get('link') or SOURCE_URL
            records.extend(parse_standard_page(text, page_url))
            if page.get('slug') == 'temporadas':
                records.extend(parse_old_seasons(text, page_url))
        except (AttributeError, TypeError, ValueError) as error:
            log_message(
                'Failed to parse Orquesta Sinfónica de Burgos season page',
                event='crawler_page_failed',
                level='warning',
                url=page.get('link'),
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class OrquestaSinfonicaDeBurgosComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestasinfonicadeburgos_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    OrquestaSinfonicaDeBurgosComCrawler().run()


if __name__ == '__main__':
    main()
