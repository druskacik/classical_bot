import re
from datetime import datetime
from urllib.parse import quote

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://corsicacantabile.com/'
SOURCE = 'Corsica Cantabile'
API_URL = 'https://admin.corsicacantabile.com/api/concerts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

VENUE_CITIES = {
    'Couvent Saint-François de Vico': 'Vico',
    'Église de Calacuccia': 'Calacuccia',
    'Église de Murzo': 'Murzo',
    'Église de Piana': 'Piana',
    'Église grecque de Cargèse': 'Cargèse',
    'Église latine de Carghjese': 'Cargèse',
    "Forêt d'Aitone - Évisa": 'Évisa',
    'Salle Cortot, Paris': 'Paris',
    'Théâtre Empire': 'Ajaccio',
    'Théâtre de verdure de Cargèse': 'Cargèse',
}

# A few archived records predate the site's structured location relation. Their
# own first-party descriptions still state an unambiguous venue and city.
ARCHIVE_LOCATIONS = {
    '2022-sonates-et-variations-violoncelle-piano': ('Église de Vico', 'Vico'),
    '2022-cloture-du-festival': ('Église de Piana', 'Piana'),
    'concert-12': ('Place de la mairie de Villanova', 'Villanova'),
}


def clean_text(value):
    if not isinstance(value, str):
        return ''
    value = value.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def extract_api_token(html):
    match = re.search(r'strapiApi["\']?\s*:\s*["\']([^"\']+)', html)
    if not match:
        raise ValueError('Could not find the public API token in the site configuration')
    return match.group(1)


def parse_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_location(item):
    location = item.get('lieux') or {}
    venue = clean_text(location.get('location_name'))
    city = VENUE_CITIES.get(venue)
    if venue and city:
        return venue, city
    return ARCHIVE_LOCATIONS.get(item.get('slug'))


def build_description(item):
    parts = []
    for field in ('short_description', 'description'):
        text = clean_text(item.get(field))
        if text and text not in parts:
            parts.append(text)

    programme = []
    for work in item.get('program_list') or []:
        composer = clean_text(work.get('composer'))
        artwork = clean_text(work.get('artwork_name'))
        line = ' — '.join(part for part in (composer, artwork) if part)
        if line:
            programme.append(line)
    if programme:
        parts.append('Programme :\n' + '\n'.join(programme))
    return '\n\n'.join(parts) or None


def event_url(item):
    edition = item.get('edition') or {}
    saison = item.get('saison') or {}
    slug = quote(item['slug'], safe='')
    if edition.get('slug'):
        return f'{SOURCE_URL}festival/{quote(edition["slug"], safe="")}/{slug}'
    if saison.get('slug'):
        return f'{SOURCE_URL}concerts/{quote(saison["slug"], safe="")}/{slug}'

    # These two records have a missing parent relation in Strapi but remain
    # published under the corresponding archive/current route.
    if item.get('slug') == '2024-musica-di-u-mondu':
        return f'{SOURCE_URL}festival/2024/{slug}'
    if item.get('slug') == 'concert-2027':
        return f'{SOURCE_URL}concerts/2026-2027/{slug}'
    return None


def parse_item(item):
    title = clean_text(item.get('title'))
    date_time = parse_datetime(item.get('scheduled_at'))
    location = parse_location(item)
    url = event_url(item)
    if not title or not date_time or not location or not url:
        return None

    event_date, time_from = date_time
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': build_description(item),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CorsicaCantabileComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='corsicacantabile_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
            token = extract_api_token(home_response.text)
            session.headers.update({
                'Authorization': f'Bearer {token}',
                'Referer': SOURCE_URL,
            })

            items = []
            page = 1
            while True:
                response = session.get(
                    API_URL,
                    params={
                        'locale': 'fr',
                        'populate': '*',
                        'sort': 'scheduled_at:asc',
                        'pagination[page]': page,
                        'pagination[pageSize]': 100,
                    },
                    timeout=45,
                )
                response.raise_for_status()
                payload = response.json()
                items.extend(payload.get('data') or [])
                pagination = (payload.get('meta') or {}).get('pagination') or {}
                if page >= pagination.get('pageCount', page):
                    break
                page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Corsica Cantabile concerts',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for item in items:
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped Corsica Cantabile concert with incomplete location or route',
                    event='crawler_record_skipped',
                    level='warning',
                    url=event_url(item) or SOURCE_URL,
                )
        return records


def main():
    CorsicaCantabileComCrawler().run()


if __name__ == '__main__':
    main()
