import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sinfonicamalaga.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/evento'
SOURCE = 'Orquesta Sinfónica de Málaga'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
    'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9,
    'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}
KNOWN_CITIES = (
    'Málaga', 'Antequera', 'Ronda', 'Nerja', 'Marbella', 'Mijas',
    'Las Lagunas de Mijas', 'Campillos', 'Fuengirola', 'Torremolinos',
    'Vélez-Málaga', 'Alhaurín de la Torre', 'Jerez de la Frontera', 'Jerez',
    'Granada', 'Córdoba', 'Sevilla', 'Madrid', 'Cádiz', 'Jaén', 'Almería',
)


def clean_text(value):
    if not value:
        return ''
    raw = unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    if '<' in raw:
        raw = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
    raw = re.sub(r'[ \t]+', ' ', raw)
    raw = re.sub(r' *\n *', '\n', raw)
    return re.sub(r'\n{3,}', '\n\n', raw).strip()


def api_items(session):
    items = []
    page = 1
    while True:
        response = session.get(API_URL, params={'per_page': 100, 'page': page}, timeout=60)
        response.raise_for_status()
        items.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', 1)):
            return items
        page += 1


def labelled_value(soup, labels):
    pattern = re.compile(rf'^\s*(?:{"|".join(labels)})\s*[:\-]?\s*(.+)$', re.I)
    for element in soup.select('li, p'):
        text = clean_text(element.get_text(' ', strip=True))
        match = pattern.match(text)
        if match:
            return re.split(
                r'\s+(?=(?:Fecha|Hora(?:rio)?|Lugar|Ubicaci[oó]n|Entradas?|Festival|Ciclo)\s*:)',
                match.group(1), maxsplit=1, flags=re.I,
            )[0].strip(' .–-')
    # Some editor versions put several labelled fields in one paragraph.
    text = clean_text(soup.get_text('\n', strip=True))
    match = re.search(
        rf'(?:^|\n)(?:{"|".join(labels)})\s*[:\-]?\s*(.+)', text, re.I,
    )
    return match.group(1).strip() if match else ''


def parse_date(value, fallback_year, fallback_month=None):
    match = re.search(
        r'\b(\d{1,2})\s+(?:de\s+)?('
        + '|'.join(MONTHS)
        + r')(?:\s+(?:de\s+)?(20\d{2}))?\b',
        value, re.I,
    )
    if not match:
        return None
    year = int(match.group(3) or fallback_year)
    # Posts entered just after New Year sometimes describe the preceding
    # December concert without repeating its year.
    if not match.group(3) and fallback_month == 1 and MONTHS[match.group(2).casefold()] == 12:
        year -= 1
    try:
        return date(year, MONTHS[match.group(2).casefold()], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*h?\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def city_from_text(value):
    folded = value.casefold()
    # Longest first prevents "Mijas" winning over "Las Lagunas de Mijas".
    matches = [
        city for city in sorted(KNOWN_CITIES, key=len, reverse=True)
        if re.search(rf'\b{re.escape(city.casefold())}\b', folded)
    ]
    # Málaga is often appended as the province after the actual municipality.
    non_capital = [city for city in matches if city != 'Málaga']
    if non_capital:
        return max(non_capital, key=lambda city: folded.rfind(city.casefold()))
    return matches[0] if matches else None


def parse_location(value):
    value = clean_text(value)
    city = city_from_text(value)
    if not city:
        return None, None
    venue = value
    # Remove address-like suffixes and locality qualifiers, but retain the
    # actual institution name (for example "Catedral de Málaga").
    venue = re.sub(
        r'\s*[–-]\s*(?:C\.?/|Calle|Plaza|Paseo|Av\.?|Diputaci[oó]n\b)[^,]*(?:,.*)?$',
        '', venue, flags=re.I,
    )
    venue = re.sub(r'\s*\([^)]*(?:\d{1,3}|C\.?/|Calle|Plaza)[^)]*\)\s*$', '', venue, flags=re.I)
    venue = re.split(r',\s*(?=(?:C\.?/|Calle|Plaza|Paseo|Av\.?))', venue, maxsplit=1, flags=re.I)[0]
    venue = re.sub(rf',\s*(?:Diputaci[oó]n de\s+)?{re.escape(city)}\s*$', '', venue, flags=re.I)
    venue = venue.strip(' ,.–-')
    return (venue or None), city


def make_record(item):
    title = clean_text((item.get('title') or {}).get('rendered'))
    url = clean_text(item.get('link'))
    html = (item.get('content') or {}).get('rendered') or ''
    soup = BeautifulSoup(html, 'html.parser')
    description = clean_text(html)
    post_date = item.get('date') or ''
    fallback_year = int(post_date[:4] or date.today().year)
    fallback_month = int(post_date[5:7]) if len(post_date) >= 7 else None

    date_text = labelled_value(soup, ('Fecha', 'Fecha y lugar'))
    concert_date = parse_date(date_text, fallback_year, fallback_month)
    location_text = labelled_value(soup, ('Lugar', 'Ubicaci[oó]n'))
    venue, city = parse_location(location_text)
    if not all((title, url, concert_date, venue, city)):
        return None

    time_text = labelled_value(soup, ('Hora', 'Horario'))
    # Many entries put the hour on the same line as the date.
    time_from = parse_time(time_text) or parse_time(date_text)
    return {
        'title': title,
        'date': concert_date,
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
    for item in api_items(session):
        try:
            record = make_record(item)
        except (TypeError, ValueError) as error:
            log_message(
                'Failed to parse concert', event='crawler_item_failed', level='warning',
                url=item.get('link'), error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
    unique = {(r['url'], r['date'], r['time_from'], r['venue']): r for r in records}
    return sorted(unique.values(), key=lambda r: (r['date'], r['time_from'] or '', r['title']))


class SinfonicamalagaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonicamalaga_com',
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
        return get_concerts()


def main():
    SinfonicamalagaComCrawler().run()


if __name__ == '__main__':
    main()
