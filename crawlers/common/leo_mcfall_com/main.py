import html
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://leo-mcfall.com/'
SOURCE = 'Leo McFall'
SCHEDULE_URL = urljoin(SOURCE_URL, 'schedule')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The schedule belongs to a touring conductor. Locations therefore need to be
# resolved per event rather than defaulting to his home country or city.
LOCATION_GEOGRAPHY = {
    'alte reithalle aarau': ('Aarau', 'CH'),
    'aristotle university': ('Thessaloniki', 'GR'),
    'aristotle university concert hall': ('Thessaloniki', 'GR'),
    'aristotle university hall': ('Thessaloniki', 'GR'),
    'carelia hall': ('Joensuu', 'FI'),
    'dortmund konzerthaus': ('Dortmund', 'DE'),
    'eglise saint martin, festival de laon': ('Laon', 'FR'),
    'emilios riadis hall': ('Thessaloniki', 'GR'),
    'esplanade concert hall': ('Singapore', 'SG'),
    'festival kassandras': ('Siviri', 'GR'),
    'festspielhaus bregenz': ('Bregenz', 'AT'),
    'festspielhaus, bregenz': ('Bregenz', 'AT'),
    'festspielhaus, grosses haus': ('Bregenz', 'AT'),
    'frauenkirche, dresden': ('Dresden', 'DE'),
    'grosses festspielhaus, salzburg': ('Salzburg', 'AT'),
    'grosses haus': ('Wiesbaden', 'DE'),
    'harpa concert hall': ('Reykjavík', 'IS'),
    "ironmonger's hall, london": ('London', 'GB'),
    'kodály music centre, pécs': ('Pécs', 'HU'),
    'kolarac, belgrade': ('Belgrade', 'RS'),
    'kuopio concert hall': ('Kuopio', 'FI'),
    'kurhaus wiesbaden': ('Wiesbaden', 'DE'),
    'kurhaus, wiesbaden': ('Wiesbaden', 'DE'),
    'kurtheater baden': ('Baden', 'CH'),
    'madetoja concert hall': ('Oulu', 'FI'),
    'marguerre saal, heidelberg': ('Heidelberg', 'DE'),
    'megaron concert hall': ('Thessaloniki', 'GR'),
    'megaron concerto hall': ('Thessaloniki', 'GR'),
    'montforthaus feldkirch': ('Feldkirch', 'AT'),
    'montforthaus, feldkirch': ('Feldkirch', 'AT'),
    'mupa concert hall, budapest': ('Budapest', 'HU'),
    'musiikkitalo, helsinki': ('Helsinki', 'FI'),
    'sala radio, bucharest': ('Bucharest', 'RO'),
    'sala sinopoli, rome': ('Rome', 'IT'),
    'sibelius hall, järvenpää': ('Järvenpää', 'FI'),
    'st nicholas church, arundel': ('Arundel', 'GB'),
    'stadthalle': ('Kassel', 'DE'),
    'stavros niarchos foundation cultural centre, athens': ('Athens', 'GR'),
    'theater darmstadt': ('Darmstadt', 'DE'),
    'thessaloniki concert hall': ('Thessaloniki', 'GR'),
    'thessaloniki concert hall - megaron': ('Thessaloniki', 'GR'),
    'thessaloniki royal theatre': ('Thessaloniki', 'GR'),
    'veria municipality arts center': ('Veria', 'GR'),
    'victoria royal concert hall': ('Victoria', 'CA'),
    'volkshaus, jena': ('Jena', 'DE'),
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n\s*\n+', '\n', value)).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_event(card):
    title_node = card.select_one('.vsel-meta-title')
    date_node = card.select_one('.vsel-meta-date span')
    location_node = card.select_one('.vsel-meta-location span')
    title = clean_text(title_node)
    event_date = parse_date(clean_text(date_node))
    venue = clean_text(location_node)
    geography = LOCATION_GEOGRAPHY.get(venue.casefold())
    if not all((title, event_date, venue, geography)):
        return None

    link = title_node.find('a', href=True) if title_node else None
    event_id = (card.get('id') or '').removeprefix('event-')
    url = urljoin(SOURCE_URL, link['href']) if link else ''
    if not url and event_id.isdigit():
        url = urljoin(SOURCE_URL, f'?post_type=event&p={event_id}')
    if not url:
        return None

    description = clean_text(card.select_one('.vsel-text')) or None
    city, country_code = geography
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


class LeoMcFallCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='leo_mcfall_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'venue', 'url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )
        session.mount('https://', HTTPAdapter(max_retries=retries))

        records = []
        page_url = SCHEDULE_URL
        visited = set()
        while page_url and page_url not in visited:
            visited.add(page_url)
            log_message('Fetching schedule page', event='crawler_url_fetch', url=page_url)
            response = session.get(page_url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            for card in soup.select('.vsel-content'):
                record = parse_event(card)
                if record:
                    records.append(record)

            next_link = next(
                (link for link in soup.select('.vsel-nav a[href]')
                 if 'next' in clean_text(link).casefold()),
                None,
            )
            page_url = urljoin(response.url, next_link['href']) if next_link else None

        log_message(
            'Schedule parsed',
            event='crawler_scrape_completed',
            record_count=len(records),
            page_count=len(visited),
        )
        return records


def main():
    LeoMcFallCrawler().run()


if __name__ == '__main__':
    main()
