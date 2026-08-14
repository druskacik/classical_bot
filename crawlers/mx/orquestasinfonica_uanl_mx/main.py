import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orquestasinfonica.uanl.mx/'
SOURCE = 'Orquesta Sinfónica de la UANL'
CITY = 'Monterrey'
COUNTRY_CODE = 'MX'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-MX,es;q=0.9,en;q=0.6',
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

NON_PROGRAM_PATHS = {
    '', 'administracion', 'articulos-promocionales', 'contacto',
    'osuanl-antecedentes', 'osuanl-integrantes', 'temporadas',
}

VENUE_WORDS = re.compile(
    r'^(?:el\s+)?(?:teatro|auditorio|arena|aula magna|museo|explanada|'
    r'parroquia|bas[ií]lica|templo|capilla|palacio|sala)\b', re.I
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def page_body(soup):
    content = soup.select_one('.content_inner')
    if not content:
        return soup
    containers = content.find_all('div', class_='container', recursive=False)
    return containers[-1] if containers else content


def page_year(soup, url, fallback=None):
    heading = clean_text(soup.select_one('h1'))
    match = re.search(r'\b(20(?:1[8-9]|2\d))\b', f'{heading} {url}')
    return int(match.group(1)) if match else fallback


def parse_date(text, year):
    if not year:
        return None, None
    patterns = (
        r'\b(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)?\s*'
        r'(\d{1,2})\s+de\s+([a-záéíóúñ]+)',
        r'\b([a-záéíóúñ]+)\s+(\d{1,2})\b',
    )
    day = month = None
    match = re.search(patterns[0], text, re.I)
    if match:
        day, month = int(match.group(1)), MONTHS.get(match.group(2).lower())
    else:
        match = re.search(patterns[1], text, re.I)
        if match:
            month, day = MONTHS.get(match.group(1).lower()), int(match.group(2))
    if not day or not month:
        return None, None
    try:
        event_date = date(year, month, day).isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:h(?:oras?)?)?\b', text, re.I)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, time_from


def extract_venue(lines, date_index):
    date_line = lines[date_index]
    same_line = re.split(r'\s+[–—-]\s+', date_line)
    for part in same_line[1:]:
        part = re.sub(r'^(?:\d{1,2}[:.]\d{2}\s*(?:h(?:oras?)?)?\s*[–—-]\s*)', '', part, flags=re.I)
        if VENUE_WORDS.search(part):
            return clean_venue(part)
    for line in lines[date_index + 1:date_index + 7]:
        if len(line) <= 160 and VENUE_WORDS.search(line):
            return clean_venue(line)
    return None


def clean_venue(value):
    value = re.sub(
        r'\s*[–—-]\s*\d{1,2}(?::\d{2})?\s*(?:h(?:oras?)?)?\s*$',
        '', value, flags=re.I,
    )
    return clean_text(value).strip(' –—-')


def city_for_venue(venue, text):
    combined = f'{venue}\n{text}'
    for city in ('Monterrey', 'San Pedro Garza García', 'San Nicolás de los Garza'):
        if re.search(rf'\b{re.escape(city)}\b', combined, re.I):
            return city
    # The site describes its seasons as Monterrey performances. These named
    # home venues are all in Monterrey; unfamiliar touring venues are skipped.
    if re.search(
        r'Teatro Universitario|Arena Monterrey|Aula Magna|Auditorio Luis Elizondo|'
        r'Explanada del Colegio Civil|Capilla Alfonsina',
        venue, re.I,
    ):
        return CITY
    return None


def page_defaults(html, year):
    soup = BeautifulSoup(html, 'html.parser')
    text = clean_text(page_body(soup))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    venue = next((clean_venue(line) for line in lines if len(line) <= 160 and VENUE_WORDS.search(line)), None)
    time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:h(?:oras?)?)?\b', text, re.I)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return year, venue, time_from


def parse_event(html, url, defaults=None):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    body = page_body(soup)
    text = clean_text(body)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    year, default_venue, default_time = defaults or (None, None, None)
    resolved_year = page_year(soup, url, year)
    for index, line in enumerate(lines):
        if len(line) > 100:
            continue
        event_date, line_time = parse_date(line, resolved_year)
        if not event_date:
            continue
        venue = extract_venue(lines, index) or default_venue
        city = city_for_venue(venue, text) if venue else None
        if not title or not venue or not city:
            continue
        all_time = parse_date(text, resolved_year)[1]
        return {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': line_time or all_time or default_time,
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
    return None


def local_page_url(href, base_url=SOURCE_URL):
    url = urljoin(base_url, href or '')
    parsed = urlparse(url)
    if parsed.netloc != urlparse(SOURCE_URL).netloc:
        return ''
    path = parsed.path.strip('/')
    if path in NON_PROGRAM_PATHS or path.startswith(('wp-', 'feed')):
        return ''
    return f'{SOURCE_URL}{path}/'


def discovery_pages(html):
    soup = BeautifulSoup(html, 'html.parser')
    pages = set()
    for anchor in soup.select('nav a, .main_menu a, .mobile_menu a'):
        text = clean_text(anchor)
        url = local_page_url(anchor.get('href'))
        if url and re.search(r'20(?:1[8-9]|2\d)|temporada|serie|concierto|[oó]pera', f'{text} {url}', re.I):
            pages.add(url)
    return pages


def linked_programs(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    body = page_body(soup)
    links = set()
    for anchor in body.select('a[href]'):
        url = local_page_url(anchor.get('href'), page_url)
        if url and url != page_url:
            links.add(url)
    return links


def fetch(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


class OrquestaSinfonicaUanlMxCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestasinfonica_uanl_mx',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        home_html = fetch(session, SOURCE_URL)
        queue = {url: None for url in discovery_pages(home_html)}
        fetched = {}

        # Archive/season pages are few and link to the individual programmes.
        for url in sorted(queue):
            try:
                html = fetch(session, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to inspect OSUANL archive page',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            year = page_year(BeautifulSoup(html, 'html.parser'), url)
            defaults = page_defaults(html, year)
            fetched[url] = (html, defaults)
            for linked_url in linked_programs(html, url):
                queue.setdefault(linked_url, defaults)

        missing = {url: year for url, year in queue.items() if url not in fetched}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch, session, url): (url, defaults) for url, defaults in missing.items()}
            for future in as_completed(futures):
                url, defaults = futures[future]
                try:
                    fetched[url] = (future.result(), defaults)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape OSUANL concert page',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records = []
        for url, (html, defaults) in fetched.items():
            record = parse_event(html, url, defaults)
            if record:
                records.append(record)
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    OrquestaSinfonicaUanlMxCrawler().run()


if __name__ == '__main__':
    main()
