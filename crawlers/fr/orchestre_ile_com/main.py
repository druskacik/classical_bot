import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestre-ile.com/'
API_URL = f'{SOURCE_URL}api/__strapi__/concerts'
SOURCE = "Orchestre national d'Île-de-France"

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

PARAMS = {
    'locale': 'fr-FR',
    'pagination[pageSize]': 100,
    'sort[0]': 'createdAt:asc',
    'populate[saison_concert]': 'true',
    'populate[genre_concert]': 'true',
    'populate[type_audience_concert]': 'true',
    'populate[programme_concert]': 'true',
    'populate[representation_concert][populate]': 'lieu_representation',
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def rich_text(value):
    """Flatten Strapi rich-text nodes while retaining paragraph boundaries."""
    if not value:
        return ''
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        parts = [rich_text(item) for item in value]
        return '\n'.join(part for part in parts if part)
    if not isinstance(value, dict):
        return clean_text(value)
    if 'text' in value:
        return clean_text(value.get('text'))
    return rich_text(value.get('children') or [])


def parse_city(address):
    address = clean_text(address)
    # Venue records consistently store French locations as "City (department)".
    match = re.match(r'^(.+?)\s*\(\s*(?:\d{2,3}|2[AB])\s*\)\s*$', address)
    return clean_text(match.group(1)) if match else ''


def parse_datetime(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        local = parsed.astimezone(ZoneInfo('Europe/Paris'))
    except (TypeError, ValueError):
        return None, None
    return local.date().isoformat(), local.strftime('%H:%M')


def programme_text(concert):
    works = []
    for item in concert.get('programme_concert') or []:
        composer = clean_text(item.get('compositeur_programme'))
        work = clean_text(item.get('oeuvre_programme'))
        piece = clean_text(item.get('piece_programme'))
        title = ' — '.join(part for part in (composer, work, piece) if part)
        if title:
            works.append(title)
    return '\n'.join(works)


def description_text(concert):
    parts = []
    for value in (
        concert.get('resume_concert'),
        concert.get('sous_titre_concert'),
        rich_text(concert.get('description_concert')),
    ):
        text = clean_text(value) if not isinstance(value, str) else value.strip()
        if text and text not in parts:
            parts.append(text)
    programme = programme_text(concert)
    if programme:
        parts.append(f'Programme\n{programme}')
    return '\n\n'.join(parts) or None


def make_record(concert, representation):
    title = clean_text(concert.get('titre_concert'))
    slug = clean_text(concert.get('slug_concert'))
    location = representation.get('lieu_representation') or {}
    venue = clean_text(location.get('nom_lieu'))
    city = parse_city(location.get('adresse_lieu'))
    event_date, time_from = parse_datetime(
        representation.get('date_debut_representation')
    )
    if not title or not slug or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': f'{SOURCE_URL}concerts/concert-{quote(slug, safe="-")}?from=agenda',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': description_text(concert),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_concerts(session):
    concerts = []
    page = 1
    while True:
        params = {**PARAMS, 'pagination[page]': page}
        response = session.get(API_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        concerts.extend(payload.get('data') or [])
        pagination = (payload.get('meta') or {}).get('pagination') or {}
        if page >= pagination.get('pageCount', page):
            return concerts
        page += 1


class OrchestreIleComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestre_ile_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        concerts = fetch_concerts(session)
        records = []
        for concert in concerts:
            for representation in concert.get('representation_concert') or []:
                record = make_record(concert, representation)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete ONDIF concert representation',
                        event='crawler_item_skipped',
                        level='warning',
                        url=SOURCE_URL,
                        error_type='IncompleteEventData',
                        error_message=(
                            'Required title, date, URL slug, venue, or city is missing'
                        ),
                    )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    OrchestreIleComCrawler().run()


if __name__ == '__main__':
    main()
