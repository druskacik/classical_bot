import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orquestraouropreto.com.br/site/'
SOURCE = 'Orquestra Ouro Preto'
API_URL = f'{SOURCE_URL}wp-json/stec/v5/events/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
    'Referer': SOURCE_URL,
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_title_and_city(value):
    match = re.match(r'^\s*([^/]+?)/[A-Z]{2}\s*[-–—]\s*(.+?)\s*$', value)
    if not match:
        return None
    city = re.sub(r'\s+', ' ', match.group(1)).strip().title()
    city = re.sub(
        r'\b(?:Da|Das|De|Do|Dos|E)\b',
        lambda particle: particle.group(0).lower(),
        city,
    )
    title = re.sub(r'\s+', ' ', match.group(2)).strip()
    if not city or not title:
        return None
    return title, city


def parse_venue(value):
    venue = clean_text(value)
    if not venue:
        return None

    # Calendar summaries sometimes append a street address, event name, or
    # directions after the actual venue. Keep only the named place.
    venue = re.split(r'\.\s+(?=(?:Rua|Avenida|Av\.|Praça)\b)', venue, maxsplit=1)[0]
    venue = re.split(r'\s+[-–—]\s+(?=(?:Rua|Avenida|Av\.)\b)', venue, maxsplit=1)[0]
    venue = venue.split(',', 1)[0].strip(' .;-')
    return venue or None


def parse_event(event):
    title_and_city = parse_title_and_city(clean_text(event.get('title')))
    meta = event.get('meta') or {}
    start = meta.get('start_date')
    venue = parse_venue(event.get('short_description'))
    url = event.get('permalink')
    if not title_and_city or not start or not venue or not url:
        return None

    try:
        start_at = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None

    title, city = title_and_city
    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': None if meta.get('all_day') else start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'BR',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OrquestraOuroPretoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestraouropreto_com_br',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            # Establishing a normal site session avoids intermittent CDN
            # rejection of direct requests to the WordPress endpoint.
            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
            response = session.get(
                API_URL,
                params={
                    'page': 1,
                    'context': 'event',
                    'permission_type': 'read_permission',
                    'per_page': 500,
                    'calendar': 362,
                },
                timeout=45,
            )
            response.raise_for_status()
            events = response.json()
            if not isinstance(events, list):
                raise ValueError('Event API returned an unexpected payload')
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Orquestra Ouro Preto events',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OrquestraOuroPretoCrawler().run()


if __name__ == '__main__':
    main()
