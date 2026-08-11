from datetime import date
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://conservatoire.toulouse.fr/'
SOURCE = 'Conservatoire à Rayonnement Régional de Toulouse'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/onct-events'
VENUES_API = f'{SOURCE_URL}wp-json/wp/v2/onct-event-lieu'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

# The Conservatoire calendar is Toulouse-based. These are the few venue names
# in the first-party taxonomy which explicitly identify an out-of-town stop.
VENUE_CITIES = {
    'église de castanet': 'Castanet-Tolosan',
    'eglise de castanet': 'Castanet-Tolosan',
    'gratentour': 'Gratentour',
    'pujaudran': 'Pujaudran',
    "l'escale": 'Tournefeuille',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def canonical_url(value):
    parts = urlsplit(value or '')
    if not parts.netloc:
        return ''
    return urlunsplit(('https', parts.netloc, parts.path, parts.query, ''))


def city_for_venue(venue):
    folded = venue.casefold()
    for marker, city in VENUE_CITIES.items():
        if marker in folded:
            return city
    return 'Toulouse'


def parse_event(item, venues):
    meta = item.get('meta') or {}
    title = clean_text((item.get('title') or {}).get('rendered'))
    short_description = clean_text(meta.get('onct-event-short-desc'))
    if short_description and short_description.casefold() not in title.casefold():
        title = f'{title} — {short_description}' if title else short_description

    try:
        event_date = date(
            int(meta['onct-event-year']),
            int(meta['onct-event-month']),
            int(meta['onct-event-day']),
        ).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    venue_ids = item.get('onct-event-lieu') or []
    venue = venues.get(venue_ids[0]) if venue_ids else None
    url = canonical_url(item.get('link'))
    if not title or not url or not venue:
        return None

    try:
        hour = int(meta['onct-event-hour'])
        minute = int(meta.get('onct-event-minute') or 0)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        time_from = f'{hour:02d}:{minute:02d}'
    except (KeyError, TypeError, ValueError):
        time_from = None

    description_html = meta.get('onct-event-desc') or ''
    description = clean_text(BeautifulSoup(description_html, 'html.parser')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city_for_venue(venue),
        'description': description,
    }


class ConservatoireToulouseCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='conservatoire_toulouse_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )))

    def _get_page(self, url, params):
        response = self.session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return response

    def _venues(self):
        response = self._get_page(VENUES_API, {'per_page': 100, 'hide_empty': 'false'})
        return {item['id']: clean_text(item.get('name')) for item in response.json()}

    def scrape(self):
        venues = self._venues()
        records = []
        page = 1
        while True:
            response = self._get_page(EVENTS_API, {
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
                '_fields': 'id,title,link,meta,onct-event-lieu',
            })
            items = response.json()
            for item in items:
                record = parse_event(item, venues)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipping event with incomplete required fields',
                        event='crawler_record_skipped',
                        level='warning',
                        event_id=item.get('id'),
                    )

            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        log_message(
            'Fetched Conservatoire event API',
            event='crawler_api_fetched',
            level='info',
            page_count=page,
            record_count=len(records),
        )
        return records


def main():
    return ConservatoireToulouseCrawler().run()


if __name__ == '__main__':
    main()
