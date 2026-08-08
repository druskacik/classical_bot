import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://osesp.art.br/osesp/pt/'
CALENDAR_URL = f'{SOURCE_URL}concertos-ingressos'
API_URL = 'https://sitesinstitucionais-api.azurewebsites.net/api/concerts'
SOURCE = 'Orquestra Sinfônica do Estado de São Paulo (Osesp)'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}

# The API calls this field ``location`` but supplies a venue, not a city.
# Most Osesp performances are in its home city; explicitly touring venues
# override that default. New, unrecognised touring venues are skipped.
TOURING_VENUES = {
    'Auditório Claudio Santoro': ('Campos do Jordão', 'BR'),
    "Basílica Menor de Sant'Ana": ('Santana de Parnaíba', 'BR'),
    'Carnegie Hall, New York': ('New York', 'US'),
    'Cine Teatro de Mariana': ('Mariana', 'BR'),
    'Teatro CBMM (Teatro do Centro Cultural UniAraxá)': ('Araxá', 'BR'),
    'Teatro Municipal Braz Cubas': ('Santos', 'BR'),
    'Teatro Municipal Casa da Ópera': ('Ouro Preto', 'BR'),
    'Teatro Municipal de Araraquara': ('Araraquara', 'BR'),
    'Teatro Municipal de São Carlos': ('São Carlos', 'BR'),
    'Teatro Municipal Geraldina Campos de Almeida': ('Cajamar', 'BR'),
}

HOME_VENUE_TERMS = (
    'sala são paulo', 'estação motiva cultural', 'museu catavento',
    'masp', 'ceu perus', 'mosteiro de são bento', 'paróquia ',
    'parque villa-lobos', 'pateo do collegio', 'pátio 1 da pinacoteca',
    'teatro b32', 'teatro flávio império',
    'teatro sesi paulista', 'caixa cultural',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def get_catalogue(session):
    params = {
        'locale': 'pt-BR',
        'pagination[pageSize]': 100,
        'sort': 'id:asc',
    }
    payload = get_json(session, API_URL, params)
    items = list(payload.get('data') or [])
    page_count = ((payload.get('meta') or {}).get('pagination') or {}).get('pageCount', 1)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(get_json, session, API_URL, {**params, 'pagination[page]': page})
            for page in range(2, page_count + 1)
        ]
        for future in as_completed(futures):
            items.extend(future.result().get('data') or [])
    return {str(item['id']): item.get('attributes') or {} for item in items}


def calendar_links(session, date_from, date_to):
    response = session.get(CALENDAR_URL, params={
        'De': date_from.strftime('%d.%m.%Y'),
        'Ate': date_to.strftime('%d.%m.%Y'),
    }, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return {
        urljoin(SOURCE_URL, node['href'])
        for node in soup.select('a[href*="/osesp/pt/concerto/"][href*="date="]')
    }


def complete_calendar_links(session, date_from, date_to):
    """Split capped result windows until every occurrence is exposed."""
    links = calendar_links(session, date_from, date_to)
    if len(links) < 40 or date_from >= date_to:
        return links
    midpoint = date_from + (date_to - date_from) // 2
    return (
        complete_calendar_links(session, date_from, midpoint)
        | complete_calendar_links(session, midpoint + timedelta(days=1), date_to)
    )


def resolve_location(venue, title):
    venue = clean_text(venue)
    if not venue or venue.lower() == 'outros':
        return None
    if venue in TOURING_VENUES:
        city, country_code = TOURING_VENUES[venue]
        return venue, city, country_code
    lowered = venue.lower()
    if any(term in lowered for term in HOME_VENUE_TERMS):
        return venue, 'São Paulo', 'BR'

    # Touring titles commonly use "em <city>" and provide strong evidence
    # without mistaking performers, addresses, or prose for a city.
    match = re.search(r'\bem\s+([\wÀ-ÿ]+(?:[ -][\wÀ-ÿ]+){0,3})$', title, re.I)
    if match:
        return venue, match.group(1).strip(), 'BR'
    return None


def parse_occurrence(url):
    match = re.search(r'/concerto/(\d+)', urlparse(url).path)
    raw_date = (parse_qs(urlparse(url).query).get('date') or [''])[0]
    if not match or not raw_date:
        return None
    try:
        instant = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
        local = instant.astimezone(ZoneInfo('America/Sao_Paulo'))
    except ValueError:
        return None
    return match.group(1), local


def make_record(url, attributes):
    parsed = parse_occurrence(url)
    title = clean_text(attributes.get('title'))
    location = resolve_location(attributes.get('location'), title)
    if not parsed or not title or not location:
        return None
    _, local = parsed
    venue, city, country_code = location
    parts = [clean_text(attributes.get('text'))]
    program = clean_text(attributes.get('program'))
    if program:
        parts.append(f'Programa\n{program}')
    description = '\n\n'.join(part for part in parts if part) or None
    return {
        'title': title,
        'date': local.date().isoformat(),
        'url': url,
        'time_from': local.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    catalogue = get_catalogue(session)
    years = [int(item['year']) for item in catalogue.values() if str(item.get('year', '')).isdigit()]
    if not years:
        return []

    first_year = min(years)
    last_year = max(max(years), date.today().year)
    ranges = [(date(year, 1, 1), date(year, 12, 31)) for year in range(first_year, last_year + 1)]
    links = set()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(complete_calendar_links, session, start, end): (start, end)
            for start, end in ranges
        }
        for future in as_completed(futures):
            start, end = futures[future]
            try:
                links.update(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar period',
                    event='crawler_item_failed',
                    level='warning',
                    url=CALENDAR_URL,
                    date_from=start.isoformat(),
                    date_to=end.isoformat(),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for url in links:
        parsed = parse_occurrence(url)
        attributes = catalogue.get(parsed[0]) if parsed else None
        record = make_record(url, attributes) if attributes else None
        if record:
            records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']))


class OsespArtBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osesp_art_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
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
    OsespArtBrCrawler().run()


if __name__ == '__main__':
    main()
