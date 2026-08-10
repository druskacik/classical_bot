import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sinfonia.org.do/'
SOURCE = 'Fundación Sinfonía'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-DO,es;q=0.9',
}

MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'septiembre': 9,
    'setiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}

CONCERT_TYPES = {'Concierto', 'Espectáculo', 'Festival', 'Gala', 'Recital', 'Temporada'}

SANTIAGO_VENUES = {
    'Centro León',
    'Sala La Restauración',
    'Sala La Restauración del Gran Teatro del Cibao',
}

NON_VENUES = {'República Dominicana', 'Santo Domingo'}

VENUE_ALIASES = {
    'Sala Juan Francisco García del Conservatorio Nacional de Música': (
        'sala juan francisco garcía del conservatorio nacional de música'
    ),
    'Sala Carlos Piantini del Teatro Nacional': 'sala carlos piantini',
    'Sala La Restauración del Gran Teatro del Cibao': 'sala la restauración',
}


def clean_text(element):
    if element is None:
        return ''
    value = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def make_date(day, month, year):
    try:
        return date(int(year), MONTHS[month.lower()], int(day)).isoformat()
    except (KeyError, ValueError):
        return None


def extract_dates(value):
    """Extract explicit Spanish dates, including shared-month and date ranges."""
    found = []
    pattern = r'(?<!\d)(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+de)?\s+(20\d{2})'
    for match in re.finditer(pattern, value, flags=re.IGNORECASE):
        parsed = make_date(*match.groups())
        if parsed and parsed not in found:
            found.append(parsed)

    # "2 y 3 de agosto de 2026" only gives the second date to the general regex.
    shared = re.search(
        r'(?<!\d)(\d{1,2})\s+y\s+(\d{1,2})\s+de\s+'
        r'([a-záéíóú]+)\s+de\s+(20\d{2})',
        value,
        flags=re.IGNORECASE,
    )
    if shared:
        first = make_date(shared.group(1), shared.group(3), shared.group(4))
        second = make_date(shared.group(2), shared.group(3), shared.group(4))
        found = [item for item in (first, second) if item] + [
            item for item in found if item not in (first, second)
        ]

    # In "12 de agosto al 4 de noviembre de 2026", the first year is implied.
    date_range = re.search(
        r'(\d{1,2})\s+de\s+([a-záéíóú]+)\s+al\s+'
        r'(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(20\d{2})',
        value,
        flags=re.IGNORECASE,
    )
    if date_range:
        first = make_date(date_range.group(1), date_range.group(2), date_range.group(5))
        last = make_date(date_range.group(3), date_range.group(4), date_range.group(5))
        found = [item for item in (first, last) if item] + [
            item for item in found if item not in (first, last)
        ]
    return found


def extract_yearless_dates(value, year):
    found = []
    for day, month in re.findall(
        r'(?<!\d)(\d{1,2})\s+de\s+([a-záéíóú]+)(?!\s+de\s+20\d{2})',
        value,
        flags=re.IGNORECASE,
    ):
        parsed = make_date(day, month, year)
        if parsed and parsed not in found:
            found.append(parsed)
    return found


def extract_times(value):
    results = []
    for hour, minute, meridiem in re.findall(
        r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(a\.?\s*m\.?|p\.?\s*m\.?)',
        value,
        flags=re.IGNORECASE,
    ):
        hour = int(hour)
        minute = int(minute or 0)
        if meridiem.lower().startswith('p') and hour != 12:
            hour += 12
        elif meridiem.lower().startswith('a') and hour == 12:
            hour = 0
        formatted = f'{hour:02d}:{minute:02d}'
        if formatted not in results:
            results.append(formatted)
    return results


def city_for_venue(venue):
    if venue == 'Escuela Superior de Música Reina Sofía':
        return 'Madrid'
    return 'Santiago de los Caballeros' if venue in SANTIAGO_VENUES else 'Santo Domingo'


def explicit_venue(description):
    normalized = description.lower()
    for venue, phrase in VENUE_ALIASES.items():
        if phrase in normalized:
            return venue
    return None


def choose_venue(venues, event_date, description):
    if len(venues) == 1:
        return venues[0]
    if not venues:
        return None

    day = str(int(event_date[-2:]))
    sentences = re.split(r'(?<=[.!?])\s+', description)
    nearby = next((sentence for sentence in sentences if re.search(rf'\b{day}\b', sentence)), '')
    for venue in venues:
        if venue.lower() in nearby.lower():
            return venue
    if 'santiago' in nearby.lower():
        return next((venue for venue in venues if venue in SANTIAGO_VENUES), None)
    if 'santo domingo' in nearby.lower():
        return next((venue for venue in venues if venue not in SANTIAGO_VENUES), None)
    return None


def parse_event(post, detail_html, venue_names, type_names):
    soup = BeautifulSoup(detail_html, 'html.parser')
    title = clean_text(soup.select_one('article h1')) or clean_text(
        BeautifulSoup(post['title']['rendered'], 'html.parser')
    )
    header = clean_text(soup.select_one('article .cont-date'))
    content = clean_text(soup.select_one('article .col-content'))
    api_content = clean_text(BeautifulSoup(post['content']['rendered'], 'html.parser'))
    description = content or api_content
    event_types = {type_names.get(term_id) for term_id in post.get('tipo', [])}
    if not event_types.intersection(CONCERT_TYPES):
        return []

    dates = extract_dates(header)
    # A season page's body contains every individual concert date and programme.
    if 'Temporada' in event_types or 'temporada' in title.lower():
        body_dates = extract_dates(f'{description}\n{api_content}')
        header_year = re.search(r'\b(20\d{2})\b', header)
        if len(body_dates) <= 1 and header_year:
            body_dates = extract_yearless_dates(
                f'{description}\n{api_content}', header_year.group(1)
            )
        if len(body_dates) > 1:
            dates = body_dates
    if not title or not dates:
        return []

    body_venue = explicit_venue(description)
    venues = [venue_names[item] for item in post.get('ubicacion', []) if item in venue_names]
    venues = [venue for venue in venues if venue not in NON_VENUES]
    if body_venue:
        venues = [body_venue]
    # Some otherwise complete pages omit their location taxonomy.
    if not venues:
        known = [
            name for name in venue_names.values()
            if name not in NON_VENUES and name.lower() in description.lower()
        ]
        venues = sorted(known, key=len, reverse=True)[:1]

    times = extract_times(header)
    records = []
    for index, event_date in enumerate(dates):
        venue = choose_venue(venues, event_date, description)
        if not venue:
            continue
        time_from = times[index] if len(times) == len(dates) else (times[0] if times else None)
        records.append({
            'title': title,
            'date': event_date,
            'url': post['link'],
            'time_from': time_from,
            'venue': venue,
            'city': city_for_venue(venue),
            'country_code': 'ES' if venue == 'Escuela Superior de Música Reina Sofía' else 'DO',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class SinfoniaOrgDoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonia_org_do',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DO',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            posts_response = session.get(
                f'{API_URL}/evento',
                params={'per_page': 100, 'orderby': 'date', 'order': 'desc'},
                timeout=45,
            )
            posts_response.raise_for_status()
            posts = posts_response.json()

            venue_response = session.get(f'{API_URL}/ubicacion', params={'per_page': 100}, timeout=45)
            venue_response.raise_for_status()
            venue_names = {item['id']: clean_text(item['name']) for item in venue_response.json()}

            type_response = session.get(f'{API_URL}/tipo', params={'per_page': 100}, timeout=45)
            type_response.raise_for_status()
            type_names = {item['id']: clean_text(item['name']) for item in type_response.json()}
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Fundación Sinfonía API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for post in posts:
            try:
                response = session.get(post['link'], timeout=45)
                response.raise_for_status()
                records.extend(parse_event(post, response.text, venue_names, type_names))
            except (requests.RequestException, ValueError, KeyError) as error:
                log_message(
                    'Failed to parse Fundación Sinfonía event',
                    event='crawler_event_failed',
                    level='warning',
                    url=post.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    SinfoniaOrgDoCrawler().run()


if __name__ == '__main__':
    main()
