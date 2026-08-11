import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cnsmd-lyon.fr/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/event'
SOURCE = 'CNSMD Lyon'

HEADERS = {
    # The normal site entry point is protected by Cloudflare, while the public
    # WordPress API and indexable event pages are available to search crawlers.
    'User-Agent': 'Googlebot',
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'decembre': 12,
}

# These are Lyon institutions/landmarks whose Google Maps links do not expose
# a postal address. Explicit touring venues are handled before this fallback.
LYON_VENUE_MARKERS = (
    'cnsmd lyon', 'auditorium - orchestre national de lyon',
    'amphitheatre darasse', 'salle varese', 'salle d’ensemble',
    "parc de la tete d'or", 'chapelle de la trinite', 'temple lanterne',
    'goethe-institut', 'opera de lyon', 'musee des beaux-arts de lyon',
    'ecole normale superieure', 'theatre kantor', 'chrd',
    'cathedrale saint-jean', 'bac a traille',
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    elif '<' in str(value) and '>' in str(value):
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    else:
        text = str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    value = unicodedata.normalize('NFKD', clean_text(value))
    return ''.join(char for char in value if not unicodedata.combining(char)).lower()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json(), response.headers


def listing_events(session):
    events = []
    page = 1
    while True:
        payload, headers = get_json(
            session,
            EVENTS_API,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title',
            },
        )
        events.extend(payload)
        if page >= int(headers.get('X-WP-TotalPages', page)):
            return events
        page += 1


def parse_french_date(value):
    value = normalized(value)
    match = re.search(
        r'\b(\d{1,2})\s+'
        r'(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)'
        r'\s+(\d{4})\b',
        value,
    )
    if not match:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)\b', normalized(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def location_block(soup):
    information = soup.select_one('.Event__top-informations')
    if not information:
        return '', ''
    for block in information.select(':scope > div'):
        spans = block.select('span')
        if spans and normalized(spans[0]) == 'lieu':
            venue = clean_text(spans[1]) if len(spans) > 1 else ''
            map_link = block.find('a', href=True)
            return venue, map_link.get('href', '') if map_link else ''
    return '', ''


def city_from_location(venue, map_url):
    venue_key = normalized(venue)
    decoded_url = unquote(map_url).replace('+', ' ')

    # Most detailed Google Maps URLs contain a French postal code followed by
    # the municipality. Stop before map coordinates or URL path components.
    match = re.search(r'\b\d{5}[ ,]+([^/@?]+)', decoded_url)
    if match:
        city = re.split(r'[,!&]', match.group(1), maxsplit=1)[0].strip(' ,.-')
        if city and not re.search(r'\d', city):
            return city

    # Many partner venues include their municipality directly in their name.
    match = re.search(r'[,–-]\s*([^,–-]+)$', venue)
    if match:
        candidate = clean_text(match.group(1))
        candidate_key = normalized(candidate)
        if candidate and not any(
            marker in candidate_key
            for marker in ('salle', 'amphi', 'theatre', 'cordes', 'bois', 'art choral')
        ):
            return candidate

    explicit_cities = (
        'Paris', 'Vichy', 'Clermont-Ferrand', 'Nantes', 'Tourcoing',
        'Annemasse', 'Collioure', 'Locquirec', 'Belfort', 'Villefranche',
        'Saint-Etienne', 'Montarcher', 'Chirens', 'Charbonnières-les-Bains',
    )
    for city in explicit_cities:
        if normalized(city) in venue_key or normalized(city) in normalized(decoded_url):
            return city

    if any(marker in venue_key for marker in LYON_VENUE_MARKERS):
        return 'Lyon'
    return None


def description_from_page(soup):
    parts = []
    description = soup.select_one('.Event__description')
    if description:
        parts.append(clean_text(description))
    for accordion in soup.select('.Event__accordions .accordion'):
        heading = clean_text(accordion.select_one('.accordion__header'))
        body = clean_text(accordion.select_one('.accordion__content'))
        if body:
            parts.append(f'{heading}\n{body}' if heading else body)
    return clean_text('\n\n'.join(part for part in parts if part)) or None


def records_from_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.Event__hero h1'))
    venue, map_url = location_block(soup)
    city = city_from_location(venue, map_url)
    if not title or not venue or not city:
        return []

    description = description_from_page(soup)
    records = []
    for item in soup.select('.Event__dates li'):
        date_text = clean_text(item.select_one('.Event__date span'))
        event_date = parse_french_date(date_text)
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(date_text),
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    def fetch(event):
        url = event.get('link') or ''
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return records_from_page(url, response.text)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch, event): event for event in events if event.get('link')}
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class CnsmdLyonFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cnsmd_lyon_fr',
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
        return get_concerts()


def main():
    CnsmdLyonFrCrawler().run()


if __name__ == '__main__':
    main()
