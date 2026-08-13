import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.maggiofiorentino.com/'
API_URL = urljoin(SOURCE_URL, 'sapi/')
SOURCE = 'Maggio Musicale Fiorentino'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# The API does not consistently populate recita.location_city. These values are
# used only where the venue text itself provides strong geographic evidence.
CITY_COUNTRIES = {
    'Arezzo': 'IT',
    'Firenze': 'IT',
    'Granada': 'ES',
    'Lucca': 'IT',
    'Lugano': 'CH',
    'Montecatini Terme': 'IT',
    'Orvieto': 'IT',
    'Ravello': 'IT',
    'Reggello': 'IT',
}

HOME_VENUE_MARKERS = (
    'teatro del maggio',
    'cavea del maggio',
    'piazzale vittorio gui',
)


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, path, params=None):
    url = urljoin(API_URL, path)
    response = session.get(url, params=params, timeout=120)
    response.raise_for_status()
    if response.status_code == 204:
        return None
    return response.json()


def season_slugs(menu):
    """Return every first-party performance season advertised in the menu."""
    slugs = []
    for group in menu.get('link', []):
        if not str(group.get('label', '')).casefold().startswith('stagione'):
            continue
        for link in group.get('sub_link', []):
            match = re.fullmatch(r'/event_season/([^/?#]+)/?', link.get('url') or '')
            if match and match.group(1) not in slugs:
                slugs.append(match.group(1))
    return slugs


def parse_location(recita):
    location = clean_html(recita.get('location'))
    explicit_city = clean_html(recita.get('location_city'))
    if not location:
        return None

    city = explicit_city or None
    country_code = CITY_COUNTRIES.get(city) if city else None

    if city is None:
        folded = location.casefold()
        if any(marker in folded for marker in HOME_VENUE_MARKERS):
            city, country_code = 'Firenze', 'IT'
        else:
            for candidate, candidate_country in CITY_COUNTRIES.items():
                if re.search(rf'(?<!\w){re.escape(candidate)}(?!\w)', location, re.I):
                    city, country_code = candidate, candidate_country
                    break

    if not city or not country_code:
        return None

    venue = re.sub(rf'\s*[-–|,]\s*{re.escape(city)}\s*$', '', location, flags=re.I).strip()
    # A city alone is not a defensible venue.
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def description_for(event):
    parts = []
    for key in ('description', 'excerpt', 'artists', 'artists_more'):
        text = clean_html(event.get(key))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def records_for_event(event):
    title = clean_html(event.get('name'))
    slug = event.get('slug')
    if not title or not slug:
        return []

    url = urljoin(SOURCE_URL, f'events/{slug}')
    description = description_for(event)
    records = []
    for recita in event.get('recita') or []:
        date_value = recita.get('date_start')
        if not isinstance(date_value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_value):
            continue
        try:
            # Reject impossible dates while retaining the site's ISO representation.
            date.fromisoformat(date_value)
        except ValueError:
            continue

        location = parse_location(recita)
        if location is None:
            continue
        venue, city, country_code = location
        time_from = recita.get('time_start')
        if not isinstance(time_from, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
            time_from = None

        records.append({
            'title': title,
            'date': date_value,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class MaggiofiorentinoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='maggiofiorentino_com',
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
            menu = get_json(session, 'app/menu', {'locale': 'it'})
            slugs = season_slugs(menu or {})
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Maggio Musicale Fiorentino menu',
                event='crawler_fetch_failed',
                level='error',
                url=urljoin(API_URL, 'app/menu'),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        event_slugs = []
        for season_slug in slugs:
            try:
                season = get_json(session, 'event/season', {'slug': season_slug}) or {}
                for event in season.get('events') or []:
                    event_slug = event.get('slug')
                    if event_slug and event_slug not in event_slugs:
                        event_slugs.append(event_slug)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Maggio Musicale Fiorentino season',
                    event='crawler_item_failed',
                    level='warning',
                    url=urljoin(SOURCE_URL, f'event_season/{season_slug}'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        for event_slug in event_slugs:
            url = urljoin(SOURCE_URL, f'events/{event_slug}')
            try:
                event = get_json(session, 'event', {'locale': 'it', 'slug': event_slug})
                if event:
                    records.extend(records_for_event(event))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Maggio Musicale Fiorentino event',
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
    MaggiofiorentinoComCrawler().run()


if __name__ == '__main__':
    main()
