import html
import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musicainsiemebologna.it/'
SOURCE = 'Fondazione Musica Insieme'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# Most unparented venue terms are Bologna venues. These are the exceptions
# published by this Bologna-based presenter.
VENUE_CITIES = {
    'Acetaia Giusti (MO)': 'Modena',
    'Fondazione Franco Severi di Cesena': 'Cesena',
    'Montese (MO)': 'Montese',
    'Palazzo di Varignana': 'Varignana',
    'San Lazzaro di Savena, Cantina Tomisa': 'San Lazzaro di Savena',
    'Teatro Laura Betti di Casalecchio di Reno': 'Casalecchio di Reno',
}
CITY_TERMS = {'Berlino', 'Bologna', 'Milano', 'Roma', 'Torino', 'Venezia'}
NON_VENUES = CITY_TERMS | {'Online streaming', 'Webinar'}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_pages(session, endpoint):
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{endpoint}',
            params={'per_page': 100, 'page': page},
            timeout=45,
        )
        response.raise_for_status()
        yield from response.json()
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            # The theme emits literal newlines in the JSON-LD `about` field.
            value = json.loads(node.string or node.get_text(), strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def parse_start(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def resolve_location(location_ids, locations, venue_override=None):
    terms = [locations[value] for value in location_ids if value in locations]
    if not terms:
        return None

    city = None
    venue = venue_override

    if venue:
        for term in terms:
            if term['name'] in CITY_TERMS:
                city = term['name']
                break

    for term in terms:
        parent = locations.get(term.get('parent'))
        if parent and parent['name'] in CITY_TERMS:
            city = parent['name']
            venue = term['name']
            break

    if venue is None:
        for term in terms:
            if term['name'] in CITY_TERMS:
                city = term['name']
                children = [item for item in terms if item.get('parent') == term['id']]
                if children:
                    venue = children[0]['name']
                    break

    if venue is None:
        candidates = [term['name'] for term in terms if term['name'] not in NON_VENUES]
        if not candidates:
            return None
        venue = candidates[0]
        city = VENUE_CITIES.get(venue, 'Bologna')

    if not city or not venue or venue == city:
        return None
    country_code = 'DE' if city == 'Berlino' else 'IT'
    return venue, city, country_code


def parse_event(post, soup, locations):
    schema = event_schema(soup)
    if not schema:
        return None
    start = parse_start(schema.get('startDate'))
    venue_node = soup.select_one('.gt-venue a')
    if venue_node is None:
        venue_node = soup.select_one('.gt-venue')
    venue_override = clean_text(venue_node)
    venue_override = re.sub(r'^Venue\s*', '', venue_override, flags=re.I).strip() or None
    location = resolve_location(post.get('location', []), locations, venue_override)
    title = clean_text(post.get('title', {}).get('rendered'))
    url = post.get('link')
    if not start or not location or not title or not url:
        return None

    description_html = post.get('content', {}).get('rendered', '')
    description = clean_text(BeautifulSoup(description_html, 'html.parser')) or None
    venue, city, country_code = location
    event_date, time_from = start
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MusicaInsiemeBolognaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicainsiemebologna_it',
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

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            locations = {item['id']: item for item in api_pages(session, 'location')}
            posts = list(api_pages(session, 'event'))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Musica Insieme event API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for post in posts:
            url = post.get('link')
            if not url:
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_event(post, BeautifulSoup(response.content, 'html.parser'), locations)
                if record:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to parse Musica Insieme event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    MusicaInsiemeBolognaItCrawler().run()


if __name__ == '__main__':
    main()
