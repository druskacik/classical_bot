import html
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://festivaldesintra.pt/'
EVENTS_URL = urljoin(SOURCE_URL, 'js/eventos.js')
SOURCE = 'Festival de Sintra'
YEAR_PATTERN = re.compile(r'PROGRAMA\s+(20\d{2})', re.IGNORECASE)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}
MONTHS = {
    'JANEIRO': 1, 'FEVEREIRO': 2, 'MARÇO': 3, 'ABRIL': 4,
    'MAIO': 5, 'JUNHO': 6, 'JULHO': 7, 'AGOSTO': 8,
    'SETEMBRO': 9, 'OUTUBRO': 10, 'NOVEMBRO': 11, 'DEZEMBRO': 12,
}
LOCATION_CITIES = (
    ('queluz', 'Queluz'),
    ('colares', 'Colares'),
    ('janas', 'Janas'),
    ('odrinhas', 'São Miguel de Odrinhas'),
)


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(value), 'html.parser')
    for credit in soup.select('.creditos'):
        credit.decompose()
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value, year):
    match = re.fullmatch(r'\s*(\d{1,2})\s+([A-ZÇÃÕÁÉÍÓÚ]+)\s*', value.upper())
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def city_for_venue(venue):
    lowered = venue.lower()
    for marker, city in LOCATION_CITIES:
        if marker in lowered:
            return city
    return 'Sintra'


def _event_blocks(script):
    start = script.find('const eventos = [')
    if start < 0:
        return []
    start = script.find('[', start) + 1
    blocks = []
    block_start = None
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(script)):
        char = script[index]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == '{':
            if depth == 0:
                block_start = index
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0 and block_start is not None:
                blocks.append(script[block_start:index + 1])
                block_start = None
        elif char == ']' and depth == 0:
            break
    return blocks


def _field(block, name):
    match = re.search(
        rf'\b{re.escape(name)}\s*:\s*([`\"\'])(.*?)\1\s*,',
        block,
        re.DOTALL,
    )
    return match.group(2).strip() if match else ''


def _description(block):
    parts = [clean_html(_field(block, 'texto'))]
    tabs_start = block.find('tabs:')
    tabs_source = block[tabs_start:] if tabs_start >= 0 else ''
    for match in re.finditer(
        r'titulo\s*:\s*([`\"\'])(.*?)\1\s*,\s*conteudo\s*:\s*([`\"\'])(.*?)\3',
        tabs_source,
        re.DOTALL,
    ):
        tab_title = clean_html(match.group(2)).lower()
        if tab_title in {'programa', 'sinopse'}:
            content = clean_html(match.group(4))
            if content:
                parts.append(f'{tab_title.title()}\n{content}')
    return '\n\n'.join(part for part in parts if part) or None


def _ticket_url(block):
    match = re.search(r'href=[\"\'](https?://[^\"\']+)[\"\']', _field(block, 'bilhetes'))
    return html.unescape(match.group(1)) if match else SOURCE_URL


def parse_events(script, year):
    records = []
    for block in _event_blocks(script):
        title = clean_html(_field(block, 'titulo'))
        title = re.sub(r'\s*Evento Gratuito\s*$', '', title, flags=re.IGNORECASE).strip()
        title = re.sub(r'\s*\n\s*', ' – ', title)
        event_date = parse_date(clean_html(_field(block, 'data')), year)
        venue = clean_html(_field(block, 'local'))
        time_match = re.fullmatch(r'([01]?\d|2[0-3]):[0-5]\d', clean_html(_field(block, 'hora')))
        if not title or not event_date or not venue:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': _ticket_url(block),
            'time_from': time_match.group(0) if time_match else None,
            'venue': venue,
            'city': city_for_venue(venue),
            'description': _description(block),
        })
    return records


class FestivalDeSintraPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivaldesintra_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
            year_match = YEAR_PATTERN.search(clean_html(home_response.text))
            if not year_match:
                raise ValueError('Could not determine programme year')

            events_response = session.get(EVENTS_URL, timeout=45)
            events_response.raise_for_status()
            records = parse_events(events_response.text, int(year_match.group(1)))
            if not records:
                raise ValueError('No parseable events found in programme data')
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape Festival de Sintra programme',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    FestivalDeSintraPtCrawler().run()


if __name__ == '__main__':
    main()
