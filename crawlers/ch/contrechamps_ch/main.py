import json
import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.contrechamps.ch/fr'
EVENTS_URL = urljoin(f'{SOURCE_URL}/', 'saison')
SOURCE = 'Contrechamps'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.6',
}

# The ensemble is based in Geneva, but its season includes tours.  The values
# below cover the cities present in the structured catalogue, including its
# archive, and deliberately keep the source's French spellings.
CITY_COUNTRIES = {
    'Annecy': 'FR', 'Bâle': 'CH', 'Basel': 'CH', 'Barcelone': 'ES',
    'Barcelona': 'ES', 'Bergen': 'NO', 'Berlin': 'DE',
    'Berne': 'CH', 'Bern': 'CH', 'Bruxelles': 'BE', 'Canberra': 'AU',
    'Chicoutimi': 'CA', 'Copenhague': 'DK', 'Cracovie': 'PL',
    'Darmstadt': 'DE', 'Genève': 'CH', 'Huddersfield': 'GB',
    'La Chaux-de-Fonds': 'CH', 'Lancy': 'CH', 'Lausanne': 'CH',
    'Lens': 'CH', 'Louvain': 'BE', 'Magdebourg': 'DE', 'Melbourne': 'AU',
    'Meyrin': 'CH', 'Montréal': 'CA', 'Nantes': 'FR', 'New York': 'US',
    'Nyon': 'CH', 'Orsières': 'CH', 'Oslo': 'NO', 'Paris': 'FR',
    'Porto': 'PT', 'Saicourt': 'CH', 'Strasbourg': 'FR', 'Sydney': 'AU',
    'Toronto': 'CA', 'Venise': 'IT', 'Vienne': 'AT', 'Viitasaari': 'FI',
    'Vernier': 'CH', 'Versoix': 'CH', 'Yverdon-les-Bains': 'CH',
    'Zurich': 'CH', 'Zürich': 'CH',
}

CATEGORY_LINES = {
    'abonnement', 'ciné-concert', 'concert', "concert d'abonnement",
    'concert d’abonnement', 'danse', 'dès 4 ans', 'entrée libre', 'événement',
    'lecture musicale', 'opéra', 'performance', 'prix libre', 'recherche', 'répertoire',
    'tournée', 'tout public',
}


def clean_text(value):
    if value is None:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def portable_text(blocks):
    """Flatten Sanity portable-text blocks while retaining programme lines."""
    if not blocks:
        return ''
    if isinstance(blocks, str):
        return clean_text(blocks)
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        children = block.get('children') or []
        line = ''.join(str(child.get('text') or '') for child in children)
        if line.strip():
            parts.append(clean_text(line))
    return '\n'.join(parts)


def event_description(event):
    sections = []
    for value in (event.get('description'), event.get('content')):
        text = portable_text(value)
        if text:
            sections.append(text)
    for field in ('slices', 'slicesRight'):
        for item in event.get(field) or []:
            text = portable_text(item.get('text'))
            if text:
                sections.append(text)
    information = portable_text(event.get('information'))
    if information:
        sections.append(information)
    return '\n\n'.join(dict.fromkeys(sections)) or None


def normalized(value):
    value = unicodedata.normalize('NFKD', value.casefold())
    return ''.join(char for char in value if not unicodedata.combining(char))


def find_city(location):
    folded = normalized(location)
    # Longest first prevents "York"-style partial matches and prefers the
    # municipality over a shorter name embedded in it.
    for city in sorted(CITY_COUNTRIES, key=len, reverse=True):
        token = normalized(city)
        # Do not interpret a city-named street (for example Rue de Lausanne
        # in Geneva) as the event municipality.
        without_street = re.sub(
            rf'\b(?:rue|route|avenue|quai|chemin)\s+(?:de |du |d[’\']|des )?{re.escape(token)}\b',
            '',
            folded,
        )
        if re.search(rf'(?<![\w-]){re.escape(token)}(?![\w-])', without_street):
            return city, CITY_COUNTRIES[city]
    return None


def parse_location(blocks):
    location = portable_text(blocks)
    city_result = find_city(location)
    if not location or not city_result:
        return None
    city, country_code = city_result
    lines = [clean_text(line) for line in location.splitlines() if clean_text(line)]
    categories = {normalized(value) for value in CATEGORY_LINES}
    candidates = []
    for line in lines:
        folded = normalized(line)
        if folded in categories:
            continue
        if re.search(r'\b\d{4,5}\b|\b(?:rue|route|rte|avenue|av\.|place|pl\.|bd|pass\.|carrer|strasse)\b', folded):
            continue
        if re.search(r'\b(?:canada|france|australie|danemark|norvege|etats-unis|royaume-uni)\b', folded):
            continue
        if any(term in folded for term in ('reservation', 'lieu en cours', 'gratuit')):
            continue
        city_match = re.search(
            rf'(?<![\w-]){re.escape(normalized(city))}(?![\w-])', folded
        )
        if city_match:
            # Locations frequently use "Venue, City (CC)" on one line.
            prefix = line[:city_match.start()].strip(' ,–-')
            if prefix:
                candidates.append(prefix)
            elif folded == normalized(city):
                continue
            else:
                candidates.append(line.strip(' ,'))
            continue
        candidates.append(line.strip(' ,'))
    if not candidates:
        return None

    venue_terms = (
        'hall', 'théâtre', 'theater', 'musée', 'museum', 'auditorium', 'salle',
        'église', 'eglise', 'cathédrale', 'opéra', 'conservatorium', 'conservatoire',
        'maison', 'centre', 'studio', 'arsenal', 'abbatial', 'docks', 'palexpo',
        'bibliothèque', 'fondation', 'ircam', 'cité bleue', 'les 6 toits',
    )
    venue = next(
        (line for line in candidates if any(term in normalized(line) for term in venue_terms)),
        candidates[0],
    )
    if normalized(venue) == normalized(city):
        return None
    return venue, city, country_code


def parse_date(value):
    if not value:
        return None
    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})', value)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def parse_time(value):
    if not value:
        return None
    match = re.search(r'T([01]\d|2[0-3]):([0-5]\d)', value)
    return f'{match.group(1)}:{match.group(2)}' if match else None


def event_url(event):
    slug = event.get('slug') or ''
    prefix = 'fr__saison__'
    if not slug.startswith(prefix):
        return None
    return urljoin(f'{SOURCE_URL}/', f'saison/{slug[len(prefix):]}')


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event_url(event)
    location = parse_location(event.get('location'))
    if not title or not url or not location:
        return []
    venue, city, country_code = location
    occurrences = event.get('occurences') or []
    if not occurrences:
        occurrences = [{'startdate': event.get('startdate'), 'starttime': event.get('starttime')}]

    records = []
    for occurrence in occurrences:
        event_date = parse_date(occurrence.get('startdate'))
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(occurrence.get('starttime')),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': event_description(event),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class ContrechampsChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='contrechamps_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(EVENTS_URL, headers=HEADERS, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Contrechamps season',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        payload = soup.select_one('#__NEXT_DATA__')
        if payload is None:
            raise ValueError('Contrechamps page does not contain Next.js event data')
        data = json.loads(payload.get_text())
        events = data['props']['pageProps']['data']['allEvents']

        records = [record for event in events for record in parse_event(event)]
        log_message(
            'Contrechamps catalogue scraped',
            event='crawler_scrape_completed',
            record_count=len(records),
        )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['url']))


def main():
    ContrechampsChCrawler().run()


if __name__ == '__main__':
    main()
