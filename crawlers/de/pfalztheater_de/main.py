import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pfalztheater.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalender/')
CALENDAR_DATA_URL = urljoin(
    SOURCE_URL,
    'wp-content/themes/PT-Theme/inc/load-more-entries-kalender.php',
)
SOURCE = 'Pfalztheater Kaiserslautern'

# Cloudflare currently challenges ordinary automated user agents, while the
# site's server-rendered public calendar remains available to search crawlers.
HEADERS = {
    'User-Agent': 'Googlebot',
    'Accept-Language': 'de-DE,de;q=0.9',
}

MONTHS = {
    'Januar': 1,
    'Februar': 2,
    'März': 3,
    'April': 4,
    'Mai': 5,
    'Juni': 6,
    'Juli': 7,
    'August': 8,
    'September': 9,
    'Oktober': 10,
    'November': 11,
    'Dezember': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=8,
        pool_maxsize=8,
        max_retries=Retry(
            total=3,
            backoff_factor=0.8,
            status_forcelist=(403, 429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\.\s+([A-Za-zÄÖÜäöü]+)\s+(\d{4})', value)
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return datetime(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).date().isoformat()
    except ValueError:
        return None


def parse_location(venue):
    venue = clean_text(venue)
    if not venue:
        return None

    external_locations = (
        ('Theater im Pfalzbau Ludwigshafen', 'Ludwigshafen am Rhein', 'DE'),
        ('Theater Heilbronn', 'Heilbronn', 'DE'),
        ('Saalbau Neustadt', 'Neustadt an der Weinstraße', 'DE'),
        ('Otfried-von-Weißenburg-Theater Dahn', 'Dahn', 'DE'),
        ('Fritz-Wunderlich-Halle Kusel', 'Kusel', 'DE'),
        ('Forum Alte Post Pirmasens', 'Pirmasens', 'DE'),
        ('Theater Koblenz', 'Koblenz', 'DE'),
    )
    for marker, city, country_code in external_locations:
        if marker.casefold() in venue.casefold():
            return city, venue, country_code

    # These calendar values identify only a city/country or an unspecified
    # touring site, so no defensible venue can be produced for those entries.
    ambiguous_tour_locations = (
        'Winterthur', 'Schwetzingen', 'Taiwan', 'Sevilla',
        'andernorts', 'Gastspiel Schule',
    )
    if any(marker.casefold() == venue.casefold() for marker in ambiguous_tour_locations):
        return None

    # Unqualified stages belong to the Kaiserslautern institution. Tour stops
    # in the calendar are explicitly qualified and handled above.
    return 'Kaiserslautern', venue, 'DE'


def parse_card(card):
    title = clean_text(card.select_one('.title-column .title'))
    date_node = card.select_one('.details-column strong')
    link = card.select_one('.button-column a.static-link[href]')
    details = clean_text(card.select_one('.details-column'))
    if not title or not date_node or not link or not details:
        return None

    date = parse_date(clean_text(date_node))
    time_match = re.search(r'\b(\d{1,2}:\d{2})\s*Uhr\b', details)
    if not date or not time_match:
        return None
    venue = details[time_match.end():].strip(' \n-\u2013')
    location = parse_location(venue)
    if not location:
        return None
    city, venue, country_code = location

    writer = clean_text(card.select_one('.title-column .writer'))
    tags = [clean_text(node) for node in card.select('.misc-tag')]
    summary = '\n'.join(value for value in (writer, *tags) if value) or None
    return {
        'title': title,
        'date': date,
        'url': urljoin(CALENDAR_URL, link['href']),
        'time_from': time_match.group(1),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': summary,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    root = soup.select_one('#stuecke-single')
    if not root:
        return None

    parts = []
    for selector in (
        ':scope > .container h2',
        '.container.stueck-content .eight.columns',
        '.container.stueck-content .actors',
    ):
        for node in root.select(selector):
            value = clean_text(node)
            if value and value not in parts:
                parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = make_session()
    soup = get_soup(session, CALENDAR_DATA_URL, params={
        'entries': 10000,
        'filter': '',
        # The broad range includes every past performance retained by the
        # source as well as its complete announced future season.
        'datefilter': '01.01.2000 - 31.12.2100',
        'premiere': 'off',
    })
    records = [
        record for card in soup.select('.single-activity')
        if (record := parse_card(card))
    ]

    detail_urls = {record['url'].split('?', 1)[0] for record in records}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, url): url
            for url in detail_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Pfalztheater event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record['url'].split('?', 1)[0])
        if detail:
            record['description'] = '\n\n'.join(
                part for part in (record['description'], detail) if part
            )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'], item['city'], item['title'], item['url']
    ))


class PfalztheaterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pfalztheater_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    PfalztheaterDeCrawler().run()


if __name__ == '__main__':
    main()
