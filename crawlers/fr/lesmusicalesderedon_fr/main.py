import re
import time
import unicodedata
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lesmusicalesderedon.fr/'
SOURCE = 'Les Musicales de Redon'
PROGRAMME_URL = urljoin(SOURCE_URL, 'concerts/')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}
# Festival venues are spread across the Pays de Redon. These names occur in
# the first-party location field and avoid incorrectly defaulting every event
# to the organisation's home city.
CITIES = (
    'Allaire', 'Avessac', 'Bains-sur-Oust', 'Béganne', 'Brain-sur-Vilaine', 'Carentoir',
    'La Chapelle-de-Brain', 'Fégréac', 'Guémené-Penfao', 'Langon',
    'Malansac', 'Peillac', 'Pipriac', 'Plessé', 'Redon', 'Renac', 'Rieux',
    'Saint-Ganton', 'Saint-Jacut-les-Pins', 'Saint-Jean-la-Poterie',
    'Saint-Just', 'Saint-Nicolas-de-Redon', 'Sixt-sur-Aff',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def folded(value):
    value = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in value if not unicodedata.combining(char))


def parse_datetime(value):
    text = folded(value)
    match = re.search(r'\b(\d{1,2})\s+([a-z]+)\s+(20\d{2})\b', text)
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?\b', text)
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{int(time_match.group(2) or 0):02d}'
    return event_date, time_from


def parse_location(value):
    location = re.sub(r'^\s*lieu\s*:\s*', '', clean_text(value), flags=re.I).strip(' -')
    if not location:
        return None, None
    location_folded = folded(location)
    matches = [city for city in CITIES if folded(city) in location_folded]
    if not matches:
        return None, None
    city = max(matches, key=len)
    # Keep the published location intact (including locality/postcode), since
    # stripping the locality can turn a church or château name into nonsense.
    return location, city


def detail_urls(session):
    response = session.get(PROGRAMME_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(PROGRAMME_URL, link.get('href')).split('#', 1)[0]
        path = urlparse(url).path.rstrip('/')
        if path.startswith('/concerts/') and path != '/concerts' and url not in urls:
            urls.append(url)
    return urls


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    info = soup.select_one('div.infos')
    if not info:
        return None
    title = clean_text(info.select_one('h1'))
    event_date, time_from = parse_datetime(info.select_one('h2'))
    venue, city = parse_location(info.select_one('h3.lieu'))
    if not title or not event_date or not venue or not city:
        return None

    description_node = soup.select_one('div.descriptif')
    description = clean_text(description_node) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': description,
    }


class LesMusicalesDeRedonFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lesmusicalesderedon_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )))
        records = []
        for url in detail_urls(session):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_detail(response.text, url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipping concert page with incomplete event fields',
                        event='crawler_event_skipped', level='warning', url=url,
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch concert page',
                    event='crawler_page_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
            time.sleep(0.1)
        return sorted(records, key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['url']
        ))


def main():
    return LesMusicalesDeRedonFrCrawler().run()


if __name__ == '__main__':
    main()
