import html
import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://occo.pt/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Orquestra de Câmara de Cascais e Oeiras'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}
CITY_NAMES = (
    'São Domingos de Rana', 'Paço de Arcos', 'Porto Salvo', 'Vila Nova de Gaia',
    'São Pedro do Estoril', 'São João do Estoril', 'Linda-a-Velha', 'Queijas',
    'Alcabideche', 'Carcavelos', 'Carnaxide', 'Barcarena', 'Monte Estoril',
    'Cascais', 'Oeiras', 'Estoril', 'Parede', 'Caxias', 'Sintra', 'Lisboa',
    'Coimbra', 'Queluz', 'Almeirim', 'Évora', 'Muge',
)
VENUE_CITIES = {
    'auditório municipal maestro césar batalha': 'Oeiras',
    'auditório senhora da boa nova': 'Estoril',
    'palácio dos aciprestes': 'Linda-a-Velha',
    'palácio do marquês de pombal': 'Oeiras',
    'palácio marquês de pombal': 'Oeiras',
    'paróquia do senhor jesus dos navegantes': 'Paço de Arcos',
    'paroquia do senhor jesus dos navegantes': 'Paço de Arcos',
    'anfiteatro colina de camões': 'Coimbra',
}


def clean_text(value):
    if not value:
        return ''
    value = html.unescape(value)
    if '<' not in value:
        return re.sub(r'\s+', ' ', value).strip()
    soup = BeautifulSoup(value, 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_location_page(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    block = soup.select_one('.mec-single-event-location')
    if not block:
        return None
    values = [clean_text(node.get_text(' ', strip=True)) for node in block.select('dd')]
    location = ', '.join(value for value in values if value)
    if not location:
        return None

    lowered = location.casefold()
    city = next((name for name in CITY_NAMES if name.casefold() in lowered), None)
    if city is None:
        city = next(
            (city_name for marker, city_name in VENUE_CITIES.items() if marker in lowered),
            None,
        )
    if city is None:
        postal_match = re.search(r'\b\d{4}-\d{3}\s+([^,|]+)', location)
        if postal_match:
            city = postal_match.group(1).strip(' .')
    if city is None and ',' in location:
        candidate = location.rsplit(',', 1)[1].strip()
        candidate = re.sub(r'^\d{4}(?:-\d{3})?\s*', '', candidate).strip()
        if candidate and not re.search(r'\d', candidate):
            city = candidate
    if not city:
        return None

    venue = location.split(',', 1)[0].strip()
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city


def parse_time(metadata):
    if str(metadata.get('mec_allday', '0')) == '1' or str(metadata.get('mec_hide_time', '0')) == '1':
        return None
    value = metadata.get('mec_start_datetime', '')
    for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value, fmt).strftime('%H:%M')
        except (TypeError, ValueError):
            pass
    return None


def parse_event(item, location):
    metadata = item.get('all_meta') or {}
    raw_date = metadata.get('mec_start_date', '')
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except (TypeError, ValueError):
        return None
    title = clean_text((item.get('title') or {}).get('rendered'))
    url = item.get('link', '').strip()
    if not title or not url or not location:
        return None
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(metadata),
        'venue': venue,
        'city': city,
        'description': clean_text((item.get('content') or {}).get('rendered')) or None,
    }


class OccoPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='occo_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def _get(self, session, url, *, params=None):
        last_error = None
        for attempt in range(4):
            try:
                response = session.get(url, params=params, timeout=45)
                if response.status_code not in {403, 429}:
                    response.raise_for_status()
                    return response
                last_error = requests.HTTPError(f'HTTP {response.status_code}', response=response)
            except requests.RequestException as error:
                last_error = error
            if attempt < 3:
                time.sleep(2 ** attempt)
        raise last_error

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = []
        page = 1
        total_pages = 1
        try:
            while page <= total_pages:
                response = self._get(
                    session, API_URL, params={'per_page': 100, 'page': page}
                )
                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError('Unexpected event API response')
                items.extend(payload)
                page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch OCCO event catalogue',
                event='crawler_fetch_failed', level='error', url=API_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        representatives = {}
        for item in items:
            location_id = str((item.get('all_meta') or {}).get('mec_location_id', '')).strip()
            if location_id:
                representatives.setdefault(location_id, []).append(item.get('link'))

        locations = {}
        for location_id, urls in representatives.items():
            for url in urls[:3]:
                if not url:
                    continue
                try:
                    time.sleep(0.5)
                    location = parse_location_page(self._get(session, url).text)
                    if location:
                        locations[location_id] = location
                        break
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch OCCO event location',
                        event='crawler_detail_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records = []
        for item in items:
            location_id = str((item.get('all_meta') or {}).get('mec_location_id', '')).strip()
            record = parse_event(item, locations.get(location_id))
            if record:
                records.append(record)
        if not records:
            raise ValueError('No OCCO events had a parseable date, venue, and city')
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OccoPtCrawler().run()


if __name__ == '__main__':
    main()
