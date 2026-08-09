import html
import re
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cnm.go.cr/'
SOURCE = 'Centro Nacional de la Música'
API_URL = 'https://cnm.go.cr/wp-json/wp/v2/posts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-CR,es;q=0.9',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
    'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9,
    'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}

# These are recurring venues whose municipality is unambiguous. The CNM also
# tours, so there is deliberately no blanket San Jose default.
VENUE_CITIES = {
    'teatro nacional': ('Teatro Nacional de Costa Rica', 'San José'),
    'teatro popular melico salazar': ('Teatro Popular Melico Salazar', 'San José'),
    'museo de arte costarricense': ('Museo de Arte Costarricense', 'San José'),
    'salon dorado': ('Salón Dorado, Museo de Arte Costarricense', 'San José'),
    'parroquia nuestra senora del sagrado corazon': (
        'Parroquia Nuestra Señora del Sagrado Corazón', 'Tibás',
    ),
    'centro nacional de la musica': ('Centro Nacional de la Música', 'Moravia'),
    'auditorio nacional': ('Auditorio Nacional', 'San José'),
    'catedral metropolitana': ('Catedral Metropolitana', 'San José'),
    'templo de la musica': ('Templo de la Música', 'San José'),
}

CITY_NAMES = {
    'san jose': 'San José', 'san josé': 'San José', 'moravia': 'Moravia',
    'tibas': 'Tibás', 'tibás': 'Tibás', 'heredia': 'Heredia',
    'alajuela': 'Alajuela', 'cartago': 'Cartago', 'escazu': 'Escazú',
    'escazú': 'Escazú', 'desamparados': 'Desamparados',
    'curridabat': 'Curridabat', 'santa ana': 'Santa Ana',
    'puntarenas': 'Puntarenas', 'limon': 'Limón', 'limón': 'Limón',
    'liberia': 'Liberia', 'grecia': 'Grecia', 'paraiso': 'Paraíso',
    'paraíso': 'Paraíso', 'turrialba': 'Turrialba', 'sarchi': 'Sarchí',
    'sarchí': 'Sarchí', 'san ramon': 'San Ramón', 'san ramón': 'San Ramón',
}

PERFORMANCE_RE = re.compile(
    r'\b(concierto|recital|ópera|opera|zarzuela|temporada oficial|presentará|'
    r'presentara|se presentará|se presentara|funciones?)\b', re.I,
)
EXCLUDE_RE = re.compile(
    r'\b(audicion(?:es)?|convocatoria|curso|taller|matrícula|matricula|'
    r'inscripción|inscripcion|concurso|comunicado|vacante)\b', re.I,
)


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(value), 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def folded(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(character)
    )


def event_dates(text, published):
    """Return explicit performance dates, preferring calendar-style wording."""
    published_date = date.fromisoformat(published[:10])
    pattern = re.compile(
        r'(?:(?:lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)\s+)?'
        r'(\d{1,2})(?:\s+y\s+(\d{1,2}))?\s+de\s+'
        r'(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|'
        r'octubre|noviembre|diciembre)(?:\s+de(?:l)?\s+(20\d{2}))?',
        re.I,
    )
    results = []
    for match in pattern.finditer(text):
        context = text[max(0, match.start() - 90):match.end() + 50]
        # Ignore press-release datelines and historical/background dates.
        if re.search(r'\bSan José,?\s+Costa Rica,?\s*$', context[:90], re.I):
            continue
        if not (re.search(r'[📅🗓]|\b(?:lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo|'
                          r'concierto|recital|función|funcion|presentará|presentara)\b', context, re.I)):
            continue
        month = MONTHS[match.group(3).lower()]
        year = int(match.group(4)) if match.group(4) else published_date.year
        if not match.group(4) and month < published_date.month - 6:
            year += 1
        for day_text in (match.group(1), match.group(2)):
            if not day_text:
                continue
            try:
                value = date(year, month, int(day_text)).isoformat()
            except ValueError:
                continue
            if value not in results:
                results.append(value)
    return results


def event_time(text):
    matches = list(re.finditer(
        r'(?:🕐|🕒|🕖|🕗|⏰|hora(?:s)?\s*:?)?\s*'
        r'\b(\d{1,2})(?::([0-5]\d))?\s*(a\.?\s*m\.?|p\.?\s*m\.?)\b', text, re.I,
    ))
    if not matches:
        match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\s*(?:horas?)?\b', text, re.I)
        return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None
    values = set()
    for match in matches:
        hour = int(match.group(1)) % 12
        if folded(match.group(3)).replace(' ', '').startswith('p'):
            hour += 12
        values.add(f'{hour:02d}:{match.group(2) or "00"}')
    return values.pop() if len(values) == 1 else None


def location_from_text(text):
    location = None
    pin = re.search(r'[📍]\s*([^\n🎟📅🕐🕒🕖🕗⏰]{3,180})', text)
    if pin:
        location = pin.group(1).strip(' .,-')

    if not location:
        normalized_text = folded(text)
        known = {
            canonical
            for venue_key, canonical in VENUE_CITIES.items()
            if venue_key in normalized_text
        }
        # Multiple venues require date-to-place association which prose news
        # releases do not express consistently; skipping is safer than pairing
        # a touring date with the wrong hall.
        if len(known) == 1:
            return known.pop()
    if not location:
        return None

    # Trim common ticket/time prose accidentally captured on the same line.
    location = re.split(r'\s+(?:Entrada|Ingreso|Boletos|Hora|A las)\b', location, 1, flags=re.I)[0]
    normalized = folded(location)
    city = None
    for key, canonical in CITY_NAMES.items():
        if re.search(rf'\b{re.escape(folded(key))}\b', normalized):
            city = canonical
            break
    if city is None:
        for venue_key, (_, venue_city) in VENUE_CITIES.items():
            if folded(venue_key) in normalized:
                city = venue_city
                break
    if city is None:
        return None
    return location, city


def parse_post(post):
    title = clean_text(post.get('title', {}).get('rendered'))
    description = clean_text(post.get('content', {}).get('rendered'))
    combined = f'{title}\n{description}'
    if not title or not description or not PERFORMANCE_RE.search(combined):
        return []
    if EXCLUDE_RE.search(title):
        return []

    dates = event_dates(description, post['date'])
    location = location_from_text(description)
    url = post.get('link')
    if not dates or not location or not url:
        return []
    venue, city = location
    time_from = event_time(description)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'CR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class CnmGoCrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cnm_go_cr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        '_fields': 'date,link,title,content',
                    },
                    timeout=45,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch CNM posts',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            if page == 1:
                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            posts = response.json()
            for post in posts:
                records.extend(parse_post(post))
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url'],
            ),
        )


def main():
    CnmGoCrCrawler().run()


if __name__ == '__main__':
    main()
