import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://petrobrasinfonica.com.br/'
PROGRAM_URL = f'{SOURCE_URL}programacao/'
EVENTS_API = f'{SOURCE_URL}wp-json/api/evento'
SOURCE = 'Orquestra Petrobras Sinfônica'
FIRST_ARCHIVE_YEAR = 2020

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'Referer': PROGRAM_URL,
    'Accept': 'application/json, text/html;q=0.9, */*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
    'sec-ch-ua-platform': '"Linux"',
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
}

# The site supplies a venue but no separate city. These fragments cover its
# published home and tour venues; title/description city names are checked too.
LOCATION_RULES = [
    ('teatro colon', 'Buenos Aires', 'AR'),
    ('teatro del libertador', 'Córdoba', 'AR'),
    ('teatro el circulo', 'Rosario', 'AR'),
    ('teatro solis', 'Montevideo', 'UY'),
    ('campina grande', 'Campina Grande', 'BR'),
    ('conservatoria', 'Conservatória', 'BR'),
    ('novo hamburgo', 'Novo Hamburgo', 'BR'),
    ('porto alegre', 'Porto Alegre', 'BR'),
    ('duque de caxias', 'Duque de Caxias', 'BR'),
    ('petropolis', 'Petrópolis', 'BR'),
    ('niteroi', 'Niterói', 'BR'),
    ('macae', 'Macaé', 'BR'),
    ('santos', 'Santos', 'BR'),
    ('blumenau', 'Blumenau', 'BR'),
    ('brasilia', 'Brasília', 'BR'),
    ('goiania', 'Goiânia', 'BR'),
    ('fortaleza', 'Fortaleza', 'BR'),
    ('recife', 'Recife', 'BR'),
    ('natal', 'Natal', 'BR'),
    ('manaus', 'Manaus', 'BR'),
    ('belem', 'Belém', 'BR'),
    ('curitiba', 'Curitiba', 'BR'),
    ('belo horizonte', 'Belo Horizonte', 'BR'),
    ('vitoria', 'Vitória', 'BR'),
    ('sao paulo', 'São Paulo', 'BR'),
    ('rio de janeiro', 'Rio de Janeiro', 'BR'),
    ('sala cecilia meireles', 'Rio de Janeiro', 'BR'),
    ('theatro municipal', 'Rio de Janeiro', 'BR'),
    ('fundicao progresso', 'Rio de Janeiro', 'BR'),
    ('cidade das artes', 'Rio de Janeiro', 'BR'),
    ('qualistage', 'Rio de Janeiro', 'BR'),
    ('mam rio', 'Rio de Janeiro', 'BR'),
    ('vivo rio', 'Rio de Janeiro', 'BR'),
    ('ccbb rio', 'Rio de Janeiro', 'BR'),
    ('sesc gloria', 'Rio de Janeiro', 'BR'),
    ('salao leopoldo miguez', 'Rio de Janeiro', 'BR'),
    ('marina da gloria', 'Rio de Janeiro', 'BR'),
    ('igreja da candelaria', 'Rio de Janeiro', 'BR'),
    ('quinta da boa vista', 'Rio de Janeiro', 'BR'),
    ('cinelandia', 'Rio de Janeiro', 'BR'),
    ('praia de ipanema', 'Rio de Janeiro', 'BR'),
    ('retiro dos artistas', 'Rio de Janeiro', 'BR'),
    ('complexo lagoon', 'Rio de Janeiro', 'BR'),
    ('teatro carlos gomes', 'Rio de Janeiro', 'BR'),
    ('teatro ecovilla', 'Rio de Janeiro', 'BR'),
    ('ecovilla ri happy', 'Rio de Janeiro', 'BR'),
    ('imperator', 'Rio de Janeiro', 'BR'),
    ('jeunesse arena', 'Rio de Janeiro', 'BR'),
    ('shopping via parque', 'Rio de Janeiro', 'BR'),
    ('catedral metropolitana', 'Rio de Janeiro', 'BR'),
    ('praca agripino grieco', 'Rio de Janeiro', 'BR'),
    ('teatro clara nunes', 'Rio de Janeiro', 'BR'),
    ('teatro riachuelo', 'Rio de Janeiro', 'BR'),
    ('espaco das americas', 'São Paulo', 'BR'),
    ('sala sao paulo', 'São Paulo', 'BR'),
    ('vibra sao paulo', 'São Paulo', 'BR'),
    ('teatro alfa', 'São Paulo', 'BR'),
    ('teatro bradesco', 'São Paulo', 'BR'),
    ('btg pactual hall', 'São Paulo', 'BR'),
    ('teatro sabesp', 'São Paulo', 'BR'),
    ('cultura artistica', 'São Paulo', 'BR'),
    ('teatro municipal bras cubas', 'Santos', 'BR'),
    ('teatro municipal jose de castro mendes', 'Campinas', 'BR'),
    ('theatro pedro ii', 'Ribeirão Preto', 'BR'),
    ('teatro opus', 'São Paulo', 'BR'),
    ('palacio das artes', 'Belo Horizonte', 'BR'),
    ('teatro guararapes', 'Recife', 'BR'),
    ('teatro boa vista', 'Recife', 'BR'),
    ('teatro rio vermelho', 'Goiânia', 'BR'),
    ('centro de convencoes ulysses', 'Brasília', 'BR'),
    ('ulysses centro de convencoes', 'Brasília', 'BR'),
    ('sesc ceilandia', 'Brasília', 'BR'),
    ('teatro positivo', 'Curitiba', 'BR'),
    ('canal da musica', 'Curitiba', 'BR'),
    ('teatro riomar', 'Fortaleza', 'BR'),
    ('teatro amazonas', 'Manaus', 'BR'),
    ('teatro da paz', 'Belém', 'BR'),
    ('teatro feevale', 'Novo Hamburgo', 'BR'),
    ('teatro michelangelo', 'Blumenau', 'BR'),
    ('arena patrick ribeiro', 'Vitória', 'BR'),
    ('espaco patrick ribeiro', 'Vitória', 'BR'),
]

NON_CONCERT_TERMS = (
    'audicao', 'audicoes', 'inscricao', 'inscricoes', 'resultado',
    'masterclass', 'masterclasses', 'concurso de regencia',
)


def normalize(value):
    text = unicodedata.normalize('NFKD', value or '')
    return ''.join(char for char in text if not unicodedata.combining(char)).lower()


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def request(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def fetch_month(session, year, month):
    return request(session, EVENTS_API, params={'ano': year, 'mes': month}).json()


def listing_events(session):
    # No events exist before 2020 in the site's available API archive. Query a
    # small future window as seasons are sometimes announced over a year ahead.
    months = [
        (year, month)
        for year in range(FIRST_ARCHIVE_YEAR, date.today().year + 3)
        for month in range(1, 13)
    ]
    events = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_month, session, year, month): (year, month)
            for year, month in months
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                payload = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape programme month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{EVENTS_API}?ano={year}&mes={month}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for item in payload if isinstance(payload, list) else []:
                if item.get('id') and item.get('permalink'):
                    events[item['id']] = item
    return list(events.values())


def resolve_location(venue, title, description):
    # Venue evidence is strongest. Only fall back to title and then the opening
    # description so tour summaries mentioning several cities cannot override
    # the location of this particular performance.
    for value in (venue, title, description[:800]):
        haystack = normalize(value)
        for fragment, city, country_code in LOCATION_RULES:
            if fragment in haystack:
                return city, country_code
    return None, None


def valid_event_date(presentation):
    try:
        return date(
            int(presentation['ano']),
            int(presentation['mes']),
            int(presentation['dia']),
        ).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def parse_detail(event, html):
    soup = BeautifulSoup(html, 'html.parser')
    details = soup.select_one('.event-details')
    if not details:
        return []

    title = clean_text(details.select_one('.datas-box > h2')) or clean_text(event.get('nome_do_evento'))
    venue = clean_text(details.select_one('.localizacao h3'))
    description = clean_text(details.select_one('.description-box > div')) or None
    normalized_title = normalize(title)
    if (
        not title
        or normalize(venue) in {'youtube', 'online'}
        or any(term in normalized_title for term in NON_CONCERT_TERMS)
    ):
        return []

    city, country_code = resolve_location(venue, title, description or '')
    if not venue or not city or not country_code:
        return []

    records = []
    for presentation in event.get('apresentacoes') or []:
        event_date = valid_event_date(presentation)
        if not event_date:
            continue
        time_from = clean_text(presentation.get('horario')) or None
        if time_from and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
            time_from = None
        records.append({
            'title': title,
            'date': event_date,
            'url': event['permalink'],
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    # Visiting the programme first mirrors the browser flow required by the
    # host's request filter before its custom JSON endpoint is accessed.
    request(session, PROGRAM_URL)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(request, session, event['permalink']): event
            for event in events
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(parse_detail(event, future.result().text))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event['permalink'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class PetrobrasSinfonicaComBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='petrobrasinfonica_com_br',
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
    PetrobrasSinfonicaComBrCrawler().run()


if __name__ == '__main__':
    main()
