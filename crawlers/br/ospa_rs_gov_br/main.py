import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ospa.rs.gov.br/inicial'
AGENDA_URL = 'https://ospa.rs.gov.br/agenda'
LIST_API = 'https://ospa.rs.gov.br/_service/conteudo/pagedlistfilho'
SOURCE = 'Orquestra Sinfônica de Porto Alegre (OSPA)'
HOME_CITY = 'Porto Alegre'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def list_params(page):
    return {
        'id': 148,
        'templatename': 'pagina.listaeventos.cards.fullheader',
        'currentPage': page,
        'pageSize': 100,
        'fields[]': ['Titulo', 'TituloCurto', 'Texto'],
        'form[palavraschave]': '',
        'form[classificacao]': '',
        # The public page defaults to today. This early date also exposes the
        # older events which remain published in the current agenda archive.
        'form[datahoraini]': '01/01/1900',
        'form[datahorafim]': '',
        'form[ordem]': 'EVENTOSANTIGOS',
    }


def get_listing_items(session):
    items = []
    page = 1
    while True:
        response = session.get(LIST_API, params=list_params(page), timeout=45)
        response.raise_for_status()
        payload = response.json()
        soup = BeautifulSoup(payload.get('body') or '', 'html.parser')
        items.extend(soup.select('.artigo__listapaginas__item'))
        if page >= int(payload.get('pagecount') or 1):
            break
        page += 1
    return items


def parse_listing_item(item):
    title_node = item.select_one('.artigo__listapaginas__item__titulo a')
    time_node = item.select_one('time[datetime]')
    venue_node = item.select_one('.conteudo-lista__item__info-evento__local')
    if not title_node or not time_node or not venue_node:
        return None
    try:
        starts_at = datetime.fromisoformat(time_node['datetime'])
    except (KeyError, ValueError):
        return None
    title = clean_text(title_node)
    venue = re.sub(r'^Local:\s*', '', clean_text(venue_node), flags=re.I)
    url = urljoin(SOURCE_URL, title_node.get('href') or '')
    if not title or not venue or not url:
        return None
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'url': url,
    }


def detail_data(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    description = clean_text(soup.select_one('.artigo__texto')) or None

    venue = ''
    location_data = soup.select_one('.mapa-locaisdata')
    if location_data:
        match = re.search(r"'txtNome':'([^']+)'", location_data.get('value') or '')
        if match:
            venue = clean_text(match.group(1))
    return description, venue


def resolve_city(venue, description):
    # The Casa da OSPA calendar is venue-specific and the site identifies its
    # address as Porto Alegre. Do not extend that default to touring venues.
    if 'casa da ospa' in venue.lower():
        return HOME_CITY
    text = description or ''
    if re.search(r'\bPorto Alegre\b', text, re.I):
        return HOME_CITY
    match = re.search(
        r'\b(?:cidade|munic[ií]pio) de ([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç -]{2,40})'
        r'(?=,|\.|\n)',
        text,
    )
    return clean_text(match.group(1)) if match else None


def make_record(item, detail=None):
    base = parse_listing_item(item)
    if not base:
        return None
    description, detail_venue = detail or (None, '')
    venue = detail_venue or base['venue']
    city = resolve_city(venue, description)
    if not city:
        return None
    return {
        'title': base['title'],
        'date': base['date'],
        'url': base['url'],
        'time_from': base['time_from'],
        'venue': venue,
        'city': city,
        'country_code': 'BR',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = get_listing_items(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for item in items:
            base = parse_listing_item(item)
            if base:
                futures[executor.submit(detail_data, session, base['url'])] = item
        for future in as_completed(futures):
            item = futures[future]
            base = parse_listing_item(item)
            try:
                detail = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=base['url'] if base else AGENDA_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                detail = None
            record = make_record(item, detail)
            if record:
                records.append(record)
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class OspaRsGovBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ospa_rs_gov_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
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
        return get_concerts()


def main():
    OspaRsGovBrCrawler().run()


if __name__ == '__main__':
    main()
