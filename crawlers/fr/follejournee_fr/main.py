import re
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://follejournee.fr/'
SOURCE = 'La Folle Journée de Nantes'
DATA_URL = 'https://static.follejournee.chapi.to/data.json'
EVENTS_URL = 'https://follejournee.fr/fr/page/tous-les-concerts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

# These two first-party programmes contain the festival performances. The
# remaining programmes are explicitly conferences and book/CD signings.
INCLUDED_PROGRAMMES = {'Programme 2026', 'Programme Kiosque'}

# The principal festival rooms are all at La Cité des Congrès in Nantes. The
# other scenes carry their municipality in their first-party scene name.
SCENE_CITIES = {
    'Kiosque C. Bechstein': 'Nantes',
    'Auditorium Apollon': 'Nantes',
    'Salle Orphée': 'Nantes',
    'Salle Cantabile': 'Nantes',
    'Salle Pizzicato': 'Nantes',
    'Salle Estampie': 'Nantes',
    'Salle Arabesque': 'Nantes',
    'Salon Vibrato': 'Nantes',
    'Salle Arpeggione': 'Nantes',
    'Espace CIC Ouest': 'Nantes',
    'Centre des expositions': 'Nantes',
    'Salon Qobuz': 'Nantes',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def local_datetime(milliseconds, timezone_name):
    try:
        return datetime.fromtimestamp(
            int(milliseconds) / 1000, ZoneInfo(timezone_name)
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def scene_city(scene):
    if scene in SCENE_CITIES:
        return SCENE_CITIES[scene]
    if ' - ' in scene:
        city = clean_text(scene.rsplit(' - ', 1)[1])
        return city or None
    return None


def parse_event(event, scenes, timezone_name):
    title = clean_text((event.get('title') or {}).get('fr'))
    scene = scenes.get(event.get('sceneId'), '')
    city = scene_city(scene)
    start = local_datetime(event.get('showStartDate'), timezone_name)
    event_id = clean_text(event.get('id'))
    if not title or not scene or not city or start is None or not event_id:
        return None

    query = urlencode({'date': start.date().isoformat(), 'event': event_id})
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': f'{EVENTS_URL}?{query}',
        'time_from': start.strftime('%H:%M'),
        'venue': scene,
        'city': city,
        'country_code': 'FR',
        'description': clean_text((event.get('description') or {}).get('fr')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FollejourneeFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='follejournee_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(DATA_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch La Folle Journée programme',
                event='crawler_fetch_failed',
                level='error',
                url=DATA_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        programme_ids = {
            item.get('_id')
            for item in payload.get('programs', [])
            if clean_text((item.get('name') or {}).get('fr')) in INCLUDED_PROGRAMMES
        }
        if not programme_ids:
            raise ValueError('First-party concert programmes were not found')

        scenes = {
            item.get('_id'): clean_text((item.get('name') or {}).get('fr'))
            for item in payload.get('scenes', [])
        }
        timezone_name = (payload.get('staticConfig') or {}).get('timezone') or 'Europe/Paris'
        records = [
            parse_event(event, scenes, timezone_name)
            for event in payload.get('events', [])
            if event.get('programId') in programme_ids
        ]
        records = [record for record in records if record]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    FollejourneeFrCrawler().run()


if __name__ == '__main__':
    main()
