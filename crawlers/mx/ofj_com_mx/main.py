import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ofj.com.mx/'
CALENDAR_URL = urljoin(SOURCE_URL, 'conciertos-eventos/')
SOURCE = 'Orquesta Filarmónica de Jalisco'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-MX,es;q=0.9,en;q=0.6',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}

# These venues have appeared repeatedly in the OFJ calendar.  The municipality
# matters because the orchestra also performs around Jalisco.
VENUE_CITIES = {
    'teatro degollado': 'Guadalajara',
    'foro larva': 'Guadalajara',
    'templo de santa teresa': 'Guadalajara',
    'ex convento del carmen': 'Guadalajara',
    'conjunto santander de artes escenicas': 'Zapopan',
    'palcco': 'Zapopan',
    'centro cultural constitucion': 'Zapopan',
    'auditorio telmex': 'Zapopan',
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = (
        BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
        if not hasattr(value, 'get_text')
        else value.get_text(separator, strip=True)
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def normalized(value):
    return value.lower().translate(str.maketrans('áéíóúüñ', 'aeiouun'))


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    # Parsing bytes lets BeautifulSoup honor the site's own charset metadata.
    return BeautifulSoup(response.content, 'html.parser')


def archive_urls(session):
    soup = get_soup(session, CALENDAR_URL)
    urls = set()
    for link in soup.select('a[href]'):
        href = link.get('href', '')
        if re.search(r'/conciertos-eventos/(?:conciertos_|opera_|ballet_|especiales_)?20\d{2}\.php', href):
            urls.add(urljoin(SOURCE_URL, href).replace('http://', 'https://'))
    # The unqualified landing page is itself an archived concert feed.
    urls.add(CALENDAR_URL)
    return sorted(urls)


def event_urls(session):
    urls = set()
    for archive_url in archive_urls(session):
        try:
            soup = get_soup(session, archive_url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape OFJ archive page',
                event='crawler_page_failed', level='warning', url=archive_url,
                error_type=type(error).__name__, error_message=str(error),
            )
            continue
        for link in soup.select('a[href*="evento/?id="]'):
            url = urljoin(SOURCE_URL, link.get('href', '')).replace('http://', 'https://')
            event_id = parse_qs(urlparse(url).query).get('id', [''])[0]
            if event_id.isdigit():
                urls.add(f'{CALENDAR_URL}evento/?id={event_id}')
    return sorted(urls, key=lambda item: int(parse_qs(urlparse(item).query)['id'][0]))


def parse_date(value):
    match = re.search(
        r'(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)\s+del?\s+(20\d{2})',
        value, re.I,
    )
    if not match:
        return None
    month = MONTHS.get(normalized(match.group(2)))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})[.:](\d{2})\s*(?:h(?:rs?)?\.?|am|pm)?', value, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    if re.search(r'\bpm\b', value, re.I) and hour < 12:
        hour += 12
    if re.search(r'\bam\b', value, re.I) and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return f'{hour:02d}:{match.group(2)}'


def resolve_city(venue, title, description):
    venue_key = normalized(venue)
    for known_venue, city in VENUE_CITIES.items():
        if known_venue in venue_key:
            return city

    # Touring entries consistently print "Municipality | 19:00 h" in their
    # first-party description.  This is stronger evidence than the home-city
    # default and avoids assigning Guadalajara to outreach performances.
    for line in description.splitlines():
        match = re.match(r'\s*([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .-]{2,40})\s*\|\s*\d{1,2}[.:]\d{2}', line)
        if match:
            return match.group(1).strip().title()

    title_key = normalized(title).strip()
    known_cities = (
        'Tlajomulco', 'Tonalá', 'Zapopan', 'Ocotlán', 'Tlaquepaque',
        'Guadalajara', 'Lagos de Moreno', 'Puerto Vallarta', 'Tepatitlán',
        'Ciudad Guzmán', 'Chapala', 'Ajijic', 'Tequila',
    )
    for city in known_cities:
        if normalized(city) in title_key:
            return city

    # OFJ's regular season is based in metropolitan Guadalajara. Unknown
    # explicitly named venues are therefore defensibly local unless the page
    # contains the touring-location pattern handled above.
    return 'Guadalajara'


def event_title(soup):
    title = clean_text(soup.select_one('#titulo-evento'))
    season = clean_text(soup.select_one('.concert_season'))
    program = clean_text(soup.select_one('.concert_program'))
    if normalized(title) in ('programa', 'evento especial', ''):
        title = ' – '.join(part for part in (season, program, title) if part)
    return title


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = event_title(soup)
    description_node = soup.select_one('#descripcion-evento')
    if description_node:
        header = description_node.select_one('#cabecera-descripcion')
        if header:
            header.extract()
    description = clean_text(description_node, '\n') or None
    records = []
    for occurrence in soup.select('#ficha-evento .fechas'):
        event_date = parse_date(clean_text(occurrence.select_one('.fecha')))
        time_from = parse_time(clean_text(occurrence.select_one('.hora')))
        venue = clean_text(occurrence.select_one('.lugar'))
        city = resolve_city(venue, title, description or '') if venue else ''
        if not title or not event_date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'MX',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.content, url)


class OfjComMxCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ofj_com_mx',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='MX',
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
        urls = event_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape OFJ concert detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    OfjComMxCrawler().run()


if __name__ == '__main__':
    main()
