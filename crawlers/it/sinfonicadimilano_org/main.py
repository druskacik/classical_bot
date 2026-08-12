import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sinfonicadimilano.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'it/eventi/')
API_URL = urljoin(SOURCE_URL, '?s=public&p=eventi&a=lista_json&lang=it')
SOURCE = 'Orchestra Sinfonica di Milano'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_url(item):
    path = f"it/eventi/{item['stagioni_slug']}/{item['slug_it']}"
    replica_id = item.get('eventi_repliche_id')
    if replica_id:
        path += f'/{replica_id}'
    return urljoin(SOURCE_URL, path)


def parse_location(value):
    location = clean_text(value)
    folded = location.casefold()
    if not location or any(term in folded for term in ('online', 'streaming', 'microsoft teams')):
        return None

    places = [
        (r'\bsalisburg', 'Salzburg', 'AT'),
        (r'\bgraz\b', 'Graz', 'AT'),
        (r'\beltville\b', 'Eltville am Rhein', 'DE'),
        (r'\bbrugg\b', 'Brugg', 'CH'),
        (r'\bluzern\b', 'Lucerne', 'CH'),
        (r'\btorino\b', 'Torino', 'IT'),
        (r'\bmonza\b', 'Monza', 'IT'),
        (r'\blecco\b', 'Lecco', 'IT'),
        (r'\bpordenone\b', 'Pordenone', 'IT'),
        (r'\bmartina franca\b', 'Martina Franca', 'IT'),
        (r'\bparma\b|auditorium niccolò paganini', 'Parma', 'IT'),
        (r'\bpavia\b', 'Pavia', 'IT'),
        (r'\bdobbiaco\b', 'Dobbiaco', 'IT'),
        (r'\btremezz|\bossuccio\b|\bisola comacina\b', 'Tremezzina', 'IT'),
        (r'\bgazzada schianno\b', 'Gazzada Schianno', 'IT'),
        (r'\bcomo\b|villa olmo', 'Como', 'IT'),
        (r'\bvarese\b', 'Varese', 'IT'),
        (r'\brovello porro\b', 'Rovello Porro', 'IT'),
        (r'\bstresa\b', 'Stresa', 'IT'),
        (r'\bravello\b|villa rufolo|san giovanni del toro', 'Ravello', 'IT'),
        (r'\bvittoriale\b', 'Gardone Riviera', 'IT'),
        (r'teatro sant.anna', 'Torino', 'IT'),
    ]
    for pattern, city, country_code in places:
        if re.search(pattern, folded):
            return location, city, country_code

    # The remaining named spaces are the orchestra's documented Milan venues
    # or other venues whose names explicitly identify Milan.
    return location, 'Milano', 'IT'


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    section = soup.select_one('section#info-block')
    if section is None:
        return None

    parts = []
    intro = section.select_one('.testo, .descrizione, [class*="description"]')
    if intro:
        parts.append(clean_text(intro))
    else:
        first_row = section.select_one('.row')
        if first_row:
            for node in first_row.find_all(recursive=False):
                if not node.select_one('.opera-div'):
                    text = clean_text(node)
                    if text:
                        parts.append(text)

    for work in section.select('.opera-div'):
        lines = [clean_text(node) for node in work.select('h4, h5')]
        lines = [line for line in lines if line]
        if lines:
            parts.append('\n'.join(lines))

    description = clean_text('\n\n'.join(parts))
    return description or None


class SinfonicaDiMilanoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonicadimilano_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def _fetch_occurrences(self, session):
        records = []
        for past in ('0', '1'):
            start = 0
            while True:
                payload = {
                    'start': start,
                    'length': 200,
                    'parametri[eventi_categorie_id]': '',
                    'parametri[mese]': '',
                    'parametri[autori_id]': '',
                    'parametri[direttori_id]': '',
                    'parametri[solisti_id]': '',
                    'parametri[stagioni_id]': '0',
                    'parametri[passati]': past,
                    'search': '',
                }
                response = session.post(API_URL, data=payload, timeout=45)
                response.raise_for_status()
                page = response.json()
                if page.get('esito') != 'ok':
                    raise ValueError(page.get('messaggio') or 'Event API returned an error')
                items = page.get('dati') or []
                records.extend(items)
                if len(items) < 200:
                    break
                start += 200
        return records

    @staticmethod
    def _detail_description(url):
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return parse_description(response.text)

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            items = self._fetch_occurrences(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Sinfonica di Milano event API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        candidates = []
        seen = set()
        for item in items:
            url = event_url(item)
            key = (item.get('eventi_repliche_id'), item.get('data_inizio'), url)
            location = parse_location(item.get('location'))
            title = clean_text(item.get('titolo_it'))
            event_date = clean_text(item.get('data_inizio'))
            try:
                date.fromisoformat(event_date)
            except ValueError:
                event_date = ''
            if key in seen or not title or not event_date or not location:
                continue
            seen.add(key)
            candidates.append((item, url, location))

        descriptions = {}
        detail_urls = {item.get('id'): url for item, url, _ in candidates}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(self._detail_description, url): event_id
                for event_id, url in detail_urls.items()
            }
            for future in as_completed(futures):
                event_id = futures[future]
                try:
                    descriptions[event_id] = future.result()
                except requests.RequestException as error:
                    descriptions[event_id] = None
                    log_message(
                        'Failed to fetch Sinfonica di Milano event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=detail_urls[event_id],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for item, url, (venue, city, country_code) in candidates:
            raw_time = clean_text(item.get('ora_inizio'))
            time_from = raw_time[:5] if re.fullmatch(r'\d{2}:\d{2}:\d{2}', raw_time) else None
            if time_from == '00:00':
                time_from = None
            records.append({
                'title': clean_text(item.get('titolo_it')),
                'date': item['data_inizio'],
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': descriptions.get(item.get('id')),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    SinfonicaDiMilanoOrgCrawler().run()


if __name__ == '__main__':
    main()
