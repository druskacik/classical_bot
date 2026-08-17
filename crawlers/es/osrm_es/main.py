import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://osrm.es/'
AJAX_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
SOURCE = 'Orquesta Sinfónica de la Región de Murcia'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}
MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}


def clean_text(node):
    if node is None:
        return ''
    value = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    value = value.replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_listing(session):
    response = session.post(
        AJAX_URL,
        data={
            'action': 'load_ajax_calendar_events',
            'postid': '0',
            'postmonth': '',
            'postyear': '',
        },
        timeout=60,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return sorted({
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('a[href]')
        if '/eventos/' in urljoin(SOURCE_URL, link['href'])
    })


def parse_date(value):
    match = re.search(
        r'\b(\d{1,2})\s+([a-záéíóúñ]+)\s+(\d{2,4})\b',
        value.lower(),
    )
    if not match or match.group(2) not in MONTHS:
        return None
    year = int(match.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime(year, MONTHS[match.group(2)], int(match.group(1))).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_place(value):
    match = re.match(r'\s*(.*?)\s*\(([^()]+)\)\s*$', value)
    if not match:
        return None, None
    venue, city = (part.strip() for part in match.groups())
    return (venue, city) if venue and city else (None, None)


def parse_detail(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title = re.sub(r'\s+', ' ', clean_text(soup.select_one('h1.elementor-heading-title')))

    fields = {}
    for node in soup.select('.fecha-post'):
        label = clean_text(node).lower()
        value = clean_text(node.select_one('.fecha-post-dato'))
        if label.startswith('fecha_'):
            fields['date'] = value
        elif label.startswith('hora_'):
            fields['time'] = value
        elif label.startswith('lugar_'):
            fields['place'] = value

    event_date = parse_date(fields.get('date', ''))
    venue, city = parse_place(fields.get('place', ''))
    programme = clean_text(soup.select_one('.content-programa'))
    # The calendar also publishes season-subscription landing pages as if they
    # were dated events. Concrete OSRM concert pages carry a programme block.
    if not title or not event_date or not venue or not city or not programme:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(fields.get('time', '')),
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': programme,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = get_listing(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    unique = {(row['url'], row['date'], row['time_from']): row for row in records}
    return sorted(unique.values(), key=lambda row: (row['date'], row['time_from'] or '', row['url']))


class OsrmEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osrm_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OsrmEsCrawler().run()


if __name__ == '__main__':
    main()
