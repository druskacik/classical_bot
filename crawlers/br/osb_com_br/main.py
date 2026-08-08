import re
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.osb.com.br/'
PROGRAM_URL = f'{SOURCE_URL}programacao/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/programacao'
SOURCE = 'Orquestra Sinfônica Brasileira (OSB)'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'abril': 4,
    'maio': 5, 'junho': 6, 'julho': 7, 'agosto': 8,
    'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12,
}


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    value = unicodedata.normalize('NFKD', value)
    return ''.join(char for char in value if not unicodedata.combining(char)).lower()


def get_catalogue(session):
    response = session.get(API_URL, params={
        'per_page': 100,
        'orderby': 'date',
        'order': 'asc',
    }, timeout=60)
    response.raise_for_status()
    items = response.json()
    page_count = int(response.headers.get('X-WP-TotalPages', '1'))
    for page in range(2, page_count + 1):
        page_response = session.get(API_URL, params={
            'per_page': 100,
            'page': page,
            'orderby': 'date',
            'order': 'asc',
        }, timeout=60)
        page_response.raise_for_status()
        items.extend(page_response.json())
    return {
        str(item['id']): {
            'url': item.get('link'),
            'year': datetime.fromisoformat(item['date']).year,
        }
        for item in items
        if item.get('id') and item.get('link') and item.get('date')
    }


def parse_date_time(value, year):
    match = re.search(
        r'(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+).*?[–-]\s*(\d{1,2})h(?:(\d{2}))?',
        clean_text(value),
        re.I,
    )
    if not match:
        return None
    month = MONTHS.get(normalized(match.group(2)))
    if not month:
        return None
    try:
        event_date = datetime(year, month, int(match.group(1))).date().isoformat()
    except ValueError:
        return None
    return event_date, f'{int(match.group(3)):02d}:{int(match.group(4) or 0):02d}'


def extract_city(service_text):
    text = clean_text(service_text)
    # OSB service blocks put the city near the end of the parenthesised address,
    # optionally followed by a two-letter state abbreviation.
    parenthetical = re.search(r'\(([^()]*)\)', text)
    address = parenthetical.group(1) if parenthetical else text
    parts = [part.strip(' .–-') for part in re.split(r'\s+[–-]\s+|,', address)]
    parts = [part for part in parts if part]
    if parts and re.fullmatch(r'[A-Z]{2}', parts[-1], re.I):
        parts.pop()
    if not parts:
        return None
    city = parts[-1]
    if re.search(r'\d|rua|avenida|av\.|praça|centro', city, re.I):
        return None
    return city


def parse_card(card, catalogue):
    classes = card.get('class') or []
    post_class = next((value for value in classes if re.fullmatch(r'post-\d+', value)), None)
    item = catalogue.get(post_class.removeprefix('post-')) if post_class else None
    if not item:
        return None

    title_node = card.select_one('[data-widget_type="theme-post-title.default"]')
    heading_nodes = card.select('[data-widget_type="heading.default"]')
    title = clean_text(title_node)
    if len(heading_nodes) < 2 or not title:
        return None
    parsed = parse_date_time(heading_nodes[0].get_text(' ', strip=True), item['year'])
    venue = clean_text(heading_nodes[1])
    if not parsed or not venue:
        return None

    service_heading = next(
        (node for node in heading_nodes if normalized(clean_text(node)).startswith('servico')),
        None,
    )
    service_widget = service_heading.find_next_sibling() if service_heading else None
    city = extract_city(service_widget.get_text(' ', strip=True) if service_widget else '')
    if not city:
        return None

    description_parts = []
    for widget in card.select('[data-widget_type]'):
        if widget is service_heading:
            break
        widget_type = widget.get('data-widget_type', '')
        if widget_type in {'text-editor.default', 'heading.default'}:
            text = clean_text(widget)
            if text and widget not in heading_nodes[:2]:
                description_parts.append(text)

    event_date, time_from = parsed
    return {
        'title': title,
        'date': event_date,
        'url': item['url'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'BR',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    catalogue = get_catalogue(session)
    response = session.get(PROGRAM_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    for card in soup.select('.type-programacao'):
        record = parse_card(card, catalogue)
        if record:
            records.append(record)
        else:
            post_class = next(
                (value for value in card.get('class', []) if re.fullmatch(r'post-\d+', value)),
                None,
            )
            log_message(
                'Skipped an incomplete OSB event',
                event='crawler_item_skipped',
                level='warning',
                url=PROGRAM_URL,
                item_id=post_class,
            )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class OsbComBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osb_com_br',
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
    OsbComBrCrawler().run()


if __name__ == '__main__':
    main()
