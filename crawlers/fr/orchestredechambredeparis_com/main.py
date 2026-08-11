import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestredechambredeparis.com/'
SOURCE = 'Orchestre de chambre de Paris'
CONCERT_SITEMAP_URL = urljoin(SOURCE_URL, 'concert-sitemap.xml')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
MONTHS = {
    'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'août': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'décembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def sitemap_urls(session):
    response = session.get(CONCERT_SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return {
        clean_text(node) for node in soup.select('url > loc')
        if '/concert/' in clean_text(node)
    }


def parse_occurrence(value):
    text = clean_text(value).casefold()
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) +
        r')\s+(\d{4})(?:\s+(\d{1,2})\s*h(?:\s*(\d{2}))?)?',
        text,
    )
    if not match:
        return None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None
    time_from = None
    if match.group(4):
        hour = int(match.group(4))
        minute = int(match.group(5) or 0)
        if hour > 23 or minute > 59:
            return None
        time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def city_from_location(venue, address):
    evidence = clean_text(f'{venue} {address}')
    # Venue pages normally expose a compact postal address such as
    # "Cité de la Musique 75019 Paris".
    postal = re.search(
        r'\b\d{5}\s+([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ\'’ -]+)', evidence
    )
    if postal:
        city = re.split(r'\n|\s{2,}|\b(?:France|Métro|Tram)\b', postal.group(1))[0]
        city = re.sub(r'\s+(?:Cedex)(?:\s+\d+)?$', '', city, flags=re.I).strip(' ,-')
        if city:
            if city.casefold() == 'paris':
                city = 'Paris'
            return city, 'FR'

    normalized = evidence.casefold()
    explicit_cities = (
        ('la côte-saint-andré', 'La Côte-Saint-André', 'FR'),
        ('la côte saint-andré', 'La Côte-Saint-André', 'FR'),
        ('amsterdam', 'Amsterdam', 'NL'),
        ('paris', 'Paris', 'FR'),
    )
    for needle, city, country_code in explicit_cities:
        if needle in normalized:
            return city, country_code
    return '', ''


def parse_detail(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('h1.event-title') or soup.find('h1'))
    location = soup.select_one('.event-intro .event-location')
    venue = clean_text(location)
    location_link = location.select_one('a[href]') if location else None
    occurrence = parse_occurrence(soup.select_one('.event-intro .event-date'))
    if not all((title, venue, occurrence)):
        return None

    address = ''
    if location_link:
        location_url = urljoin(url, location_link.get('href'))
        try:
            location_soup = get_soup(session, location_url)
            address = clean_text(location_soup.select_one('main .event-location address'))
        except requests.RequestException as error:
            log_message(
                'Failed to resolve OCP venue page',
                event='crawler_item_failed', level='warning', url=location_url,
                error_type=type(error).__name__, error_message=str(error),
            )
    city, country_code = city_from_location(venue, address)
    if not city or not country_code or venue.casefold() == city.casefold():
        log_message(
            'Skipped incomplete OCP concert',
            event='crawler_item_skipped', level='warning', url=url,
            error_type='IncompleteEventData',
            error_message='Required date, venue, city, or country is missing',
        )
        return None

    description_parts = []
    for node in soup.select('.event-info .event-item, .event-content'):
        text = clean_text(node)
        if text and text not in description_parts:
            description_parts.append(text)
    event_date, time_from = occurrence
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OrchestreDeChambreDeParisComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestredechambredeparis_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = sitemap_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(parse_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape OCP concert',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    OrchestreDeChambreDeParisComCrawler().run()


if __name__ == '__main__':
    main()
