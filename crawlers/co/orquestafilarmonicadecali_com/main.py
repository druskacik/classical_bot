import html
import re
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orquestafilarmonicadecali.com/es'
SOURCE = 'Orquesta Filarmónica de Cali'
API_URL = 'https://cms.orquestafilarmonicadecali.com/wp-json/wp/v2'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-CO,es;q=0.9',
}

CITY_PATTERNS = {
    'Cali': r'\bcali\b',
    'Buenaventura': r'\bbuenaventura\b',
    'Buga': r'\b(?:guadalajara de )?buga\b',
    'Caicedonia': r'\bcaicedonia\b',
    'Candelaria': r'\bcandelaria\b',
    'Cartago': r'\bcartago\b',
    'Dagua': r'\bdagua\b',
    'Jamundí': r'\bjamundi\b',
    'Palmira': r'\bpalmira\b',
    'Roldanillo': r'\broldanillo\b',
    'Sevilla': r'\bsevilla\b',
    'Tuluá': r'\btulua\b',
    'Yumbo': r'\byumbo\b',
    'Zarzal': r'\bzarzal\b',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    else:
        value = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def normalized(value):
    value = unicodedata.normalize('NFKD', clean_text(value))
    value = ''.join(character for character in value if not unicodedata.combining(character))
    return value.lower()


def fetch_collection(session, endpoint):
    records = []
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{endpoint}',
            params={'per_page': 100, 'page': page},
            timeout=60,
        )
        response.raise_for_status()
        batch = response.json()
        records.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return records
        page += 1


def venue_id(meta):
    associations = meta.get('crb_lugar_asociado') or []
    if not associations:
        return None
    try:
        return int(associations[0].get('id'))
    except (AttributeError, TypeError, ValueError):
        return None


def infer_city(concert, venue):
    meta = concert.get('meta') or {}
    primary_evidence = normalized(' '.join([
        concert.get('slug', ''),
        clean_text(concert.get('title', {}).get('rendered')),
        clean_text(venue.get('title', {}).get('rendered')),
        clean_text((venue.get('meta') or {}).get('crb_direccion')),
    ]))
    for city, pattern in CITY_PATTERNS.items():
        if city == 'Buenaventura' and 'teatro municipal enrique buenaventura' in primary_evidence:
            continue
        if re.search(pattern, primary_evidence):
            return city
    fallback_evidence = normalized(meta.get('crb_lugar'))
    for city, pattern in CITY_PATTERNS.items():
        if city == 'Buenaventura' and 'teatro municipal enrique buenaventura' in fallback_evidence:
            continue
        if re.search(pattern, fallback_evidence):
            return city
    # The orchestra's venue catalogue is based in Cali; touring records name
    # their municipality in the event, venue, or address fields above.
    return 'Cali'


def format_program(program):
    lines = []
    for item in program or []:
        if not isinstance(item, dict):
            continue
        composer = clean_text(item.get('compositor'))
        work = clean_text(item.get('obra'))
        heading = ' — '.join(part for part in (composer, work) if part)
        movements = [
            clean_text(movement.get('titulo'))
            for movement in item.get('movimientos') or []
            if isinstance(movement, dict) and clean_text(movement.get('titulo'))
        ]
        if heading:
            lines.append(heading)
        lines.extend(f'  {movement}' for movement in movements)
    return '\n'.join(lines)


def build_description(concert):
    meta = concert.get('meta') or {}
    parts = [
        clean_text(meta.get('crb_subtitulo')),
        clean_text(concert.get('content', {}).get('rendered')),
        clean_text(concert.get('excerpt', {}).get('rendered')),
    ]
    program = format_program(meta.get('crb_programa'))
    if program:
        parts.append(f'Programa:\n{program}')
    return '\n\n'.join(dict.fromkeys(part for part in parts if part)) or None


def event_datetimes(concert):
    meta = concert.get('meta') or {}
    try:
        primary = datetime.fromisoformat(clean_text(meta.get('crb_fecha')))
    except (TypeError, ValueError):
        return []

    values = [primary]
    subtitle = normalized(meta.get('crb_subtitulo'))
    match = re.search(
        r'doble presentacion\s+(\d{1,2})\s+y\s+(\d{1,2})\s+de\s+'
        r'(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|'
        r'octubre|noviembre|diciembre)'
        r'(?:\s+del?\s+(20\d{2}))?',
        subtitle,
    )
    if match:
        months = {
            'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
            'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
            'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
        }
        year = int(match.group(4) or primary.year)
        for day in (int(match.group(1)), int(match.group(2))):
            try:
                candidate = primary.replace(year=year, month=months[match.group(3)], day=day)
            except ValueError:
                continue
            if candidate not in values:
                values.append(candidate)
    return sorted(values)


def parse_concert(concert, venues):
    meta = concert.get('meta') or {}
    if meta.get('crb_esta_cancelado'):
        return []
    title = clean_text(concert.get('title', {}).get('rendered'))
    slug = clean_text(concert.get('slug'))
    venue = venues.get(venue_id(meta))
    slug_words = set(normalized(slug).split('-'))
    for candidate in venues.values():
        candidate_words = {
            word for word in re.findall(r'[a-z0-9]+', normalized(
                candidate.get('title', {}).get('rendered')
            ))
            if word not in {'de', 'del', 'el', 'la', 'los', 'las', 'y'}
        }
        if len(candidate_words) >= 2 and candidate_words.issubset(slug_words):
            venue = candidate
            break
    venue_name = clean_text(venue.get('title', {}).get('rendered')) if venue else ''
    venue_name = venue_name or clean_text(meta.get('crb_lugar'))
    if (
        not title or not slug or not venue_name
        or venue_name.lower() in {'por definir', 'virtual'}
    ):
        return []

    url = f'{SOURCE_URL}/conciertos/{slug}'
    city = infer_city(concert, venue or {})
    description = build_description(concert)
    return [
        {
            'title': title,
            'date': event_datetime.date().isoformat(),
            'url': url,
            'time_from': event_datetime.strftime('%H:%M'),
            'venue': venue_name,
            'city': city,
            'country_code': 'CO',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_datetime in event_datetimes(concert)
    ]


class OrquestaFilarmonicaDeCaliComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestafilarmonicadecali_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CO',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            venue_items = fetch_collection(session, 'venue')
            concert_items = fetch_collection(session, 'concert')
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Orquesta Filarmónica de Cali catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        venues = {item['id']: item for item in venue_items}
        records = []
        for concert in concert_items:
            records.extend(parse_concert(concert, venues))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    OrquestaFilarmonicaDeCaliComCrawler().run()


if __name__ == '__main__':
    main()
