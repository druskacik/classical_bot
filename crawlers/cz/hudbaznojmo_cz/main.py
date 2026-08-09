import concurrent.futures
import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hudbaznojmo.cz/'
SOURCE = 'Hudební festival Znojmo'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/akce-detail'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/json;q=0.9,*/*;q=0.8',
    'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.7',
}

# The festival holds most events in Znojmo, but also publishes performances
# around South Moravia and occasional Austrian and German tour dates. Longer
# and more specific names must precede their shorter alternatives.
LOCATIONS = [
    ('Nový Šaldorf-Sedlešovice', 'Nový Šaldorf-Sedlešovice', 'CZ'),
    ('Moravský Krumlov', 'Moravský Krumlov', 'CZ'),
    ('Mor. Krumlov', 'Moravský Krumlov', 'CZ'),
    ('Vranov nad Dyjí', 'Vranov nad Dyjí', 'CZ'),
    ('Mitterretzbach', 'Mitterretzbach', 'AT'),
    ('Unterretzbach', 'Unterretzbach', 'AT'),
    ('Petronell', 'Petronell-Carnuntum', 'AT'),
    ('Drosendorf', 'Drosendorf', 'AT'),
    ('Pražského hradu', 'Praha', 'CZ'),
    ('Praha', 'Praha', 'CZ'),
    ('Jevišovice', 'Jevišovice', 'CZ'),
    ('Jaroslavice', 'Jaroslavice', 'CZ'),
    ('Lechovice', 'Lechovice', 'CZ'),
    ('Uherčice', 'Uherčice', 'CZ'),
    ('Dobšice', 'Dobšice', 'CZ'),
    ('Písečné', 'Písečné', 'CZ'),
    ('Bantice', 'Bantice', 'CZ'),
    ('Šatov', 'Šatov', 'CZ'),
    ('Šanov', 'Šanov', 'CZ'),
    ('Hnanice', 'Hnanice', 'CZ'),
    ('Hardegg', 'Hardegg', 'AT'),
    ('Laufen', 'Laufen', 'DE'),
    ('Mašovický', 'Mašovice', 'CZ'),
    ('Znojmo', 'Znojmo', 'CZ'),
]

# These named festival venues are in Znojmo even though the city is omitted
# from their displayed place label.
ZNOJMO_VENUES = {
    'Alšovka JM Muzeum',
    'Centrum Louka',
    'Domeček',
    'Dům umění',
    'Enotéka znojemských vín',
    'Horní park',
    'Jízdárna Louckého kláštera',
    'Kavárna Amanita',
    'Kavárna Zeman',
    'Klub Harvart',
    'Lahofer',
    'Městské divadlo Znojmo',
    'Na Káře',
    'Nevoga',
    'Sklep U Císaře Zikmunda',
}


def clean_text(value):
    if not value:
        return ''
    value = unescape(str(value)).replace('\xa0', ' ').replace('\u202f', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def fetch_json(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=40)
    response.raise_for_status()
    return response


def event_items():
    items = []
    page = 1
    while True:
        response = fetch_json(
            EVENTS_API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link,title',
            },
        )
        payload = response.json()
        if not payload:
            break
        items.extend(payload)
        if page >= int(response.headers.get('X-WP-TotalPages', page)):
            break
        page += 1
    return items


def parse_date(value):
    match = re.search(r'(?<!\d)(\d{1,2})\.\s*(\d{1,2})\.\s*(20\d{2})(?!\d)', clean_text(value))
    if not match:
        return None
    try:
        return datetime(
            int(match.group(3)), int(match.group(2)), int(match.group(1))
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', clean_text(value))
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def resolve_location(place):
    place = clean_text(place).strip(' ,')
    if not place:
        return None, None, None

    for marker, city, country_code in LOCATIONS:
        if re.search(rf'(?<!\w){re.escape(marker)}(?!\w)', place, re.IGNORECASE):
            # A city alone is not a venue and must not become a placeholder.
            if place.casefold() == marker.casefold() or place.casefold() == city.casefold():
                return None, None, None
            return place, city, country_code

    normalized = place.casefold()
    if any(normalized == venue.casefold() for venue in ZNOJMO_VENUES):
        return place, 'Znojmo', 'CZ'
    return None, None, None


def description_from(soup):
    parts = []
    for selector in ('.event-detail .content-column', '.event-description'):
        node = soup.select_one(selector)
        if not node:
            continue
        for unwanted in node.select('script, style, form, button, .event-pictograms, .buttons'):
            unwanted.decompose()
        text = clean_text(node.get_text('\n', strip=True))
        if text and text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def parse_event(item):
    url = item.get('link')
    if not url:
        return None
    response = requests.get(url, headers=HEADERS, timeout=40)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    title_node = soup.select_one('.event-detail .content-column h2.name')
    date_node = soup.select_one('.event-pictograms .box.date')
    time_node = soup.select_one('.event-pictograms .box.time')
    place_node = soup.select_one('.event-pictograms .box.place')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    event_date = parse_date(date_node.get_text(' ', strip=True) if date_node else '')
    venue, city, country_code = resolve_location(
        place_node.get_text(' ', strip=True) if place_node else ''
    )
    if not title or not event_date or not venue or not city or not country_code:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': response.url,
        'time_from': parse_time(time_node.get_text(' ', strip=True) if time_node else ''),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    items = event_items()
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_event, item): item for item in items}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipping event without a valid date or location',
                        event='crawler_item_skipped',
                        level='warning',
                        url=item.get('link'),
                    )
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class HudbaZnojmoCzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hudbaznojmo_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        return get_concerts()


def main():
    HudbaZnojmoCzCrawler().run()


if __name__ == '__main__':
    main()
