import html
import re
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://onauvergne.com/'
SOURCE = 'Orchestre national Auvergne-Rhône-Alpes'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/mec-events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'janv': 1, 'jan': 1,
    'fevrier': 2, 'fevr': 2, 'fev': 2,
    'mars': 3,
    'avril': 4, 'avr': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7, 'juil': 7,
    'aout': 8,
    'septembre': 9, 'sept': 9,
    'octobre': 10, 'oct': 10,
    'novembre': 11, 'nov': 11,
    'decembre': 12, 'dec': 12,
}

# MEC location names without a comma do not carry a city separately in the
# public markup. These venue/city pairs are all first-party calendar values.
VENUE_CITIES = {
    'auditorium de lyon': 'Lyon',
    'cathedrale notre-dame de paris': 'Paris',
    'espace malraux': 'Chambéry',
    'mc2: grenoble': 'Grenoble',
    'opera de vichy': 'Vichy',
    'philharmonie de berlin - kammermusiksaal': 'Berlin',
    'philharmonie de berlin – kammermusiksaal': 'Berlin',
    'salle gaveau': 'Paris',
    'temple du bas': 'Neuchâtel',
    'theatre d’aurillac': 'Aurillac',
    "theatre d'aurillac": 'Aurillac',
    'theatre de moulins': 'Moulins',
    'theatre du puy-en-velay': 'Le Puy-en-Velay',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u202f', ' ')
    return re.sub(r'\s+', ' ', value).strip()


def folded(value):
    value = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in value if not unicodedata.combining(char))


def parse_date(value):
    text = folded(value).replace('.', ' ')
    match = re.search(r'\b(\d{1,2})\s+([a-z]+)\s+(20\d{2})\b', text)
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)', folded(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def city_from_venue(venue):
    if ',' in venue:
        city = clean_text(venue.rsplit(',', 1)[1])
        return city or None
    normalized = folded(venue)
    for known_venue, city in VENUE_CITIES.items():
        if folded(known_venue) == normalized:
            return city
    return None


def country_from_venue(venue):
    normalized = folded(venue)
    if 'berlin' in normalized:
        return 'DE'
    if 'neuchatel' in normalized:
        return 'CH'
    return 'FR'


def extract_detail(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    date_node = soup.select_one('.mec-start-date-label')
    time_node = soup.select_one('.mec-single-event-time abbr')
    venue_node = soup.select_one('.mec-single-event-location dd.author')
    venue = clean_text(venue_node)
    return {
        'date': parse_date(clean_text(date_node)),
        'time_from': parse_time(clean_text(time_node)),
        'venue': venue,
        'city': city_from_venue(venue) if venue else None,
        'country_code': country_from_venue(venue),
    }


def description_from_content(rendered):
    soup = BeautifulSoup(rendered or '', 'html.parser')
    for node in soup.select('script, style, form, nav'):
        node.decompose()
    text = clean_text(soup)
    return text or None


class OnAuvergneCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='onauvergne_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
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

    def fetch_events(self):
        page = 1
        while True:
            response = self.session.get(
                EVENTS_API,
                params={
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'asc',
                    '_fields': 'id,link,title,content',
                },
                timeout=45,
            )
            if response.status_code == 400 and page > 1:
                break
            response.raise_for_status()
            events = response.json()
            yield from events
            if page >= int(response.headers.get('X-WP-TotalPages', page)):
                break
            page += 1

    def scrape(self):
        records = []
        for event in self.fetch_events():
            url = event.get('link')
            title = clean_text(BeautifulSoup(
                event.get('title', {}).get('rendered', ''), 'html.parser'
            ))
            if not url or not title:
                continue
            try:
                response = self.session.get(url, timeout=45)
                response.raise_for_status()
                detail = extract_detail(response.text)
            except requests.RequestException as error:
                log_message(
                    'Could not fetch event detail',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            if not detail['date'] or not detail['venue'] or not detail['city']:
                log_message(
                    'Skipping event with incomplete occurrence metadata',
                    level='warning',
                    url=url,
                    has_date=bool(detail['date']),
                    has_venue=bool(detail['venue']),
                    has_city=bool(detail['city']),
                )
                continue

            records.append({
                'title': title,
                'date': detail['date'],
                'url': url,
                'time_from': detail['time_from'],
                'venue': detail['venue'],
                'city': detail['city'],
                'country_code': detail['country_code'],
                'description': description_from_content(
                    event.get('content', {}).get('rendered')
                ),
            })
        return records


def main():
    return OnAuvergneCrawler().run()


if __name__ == '__main__':
    main()
