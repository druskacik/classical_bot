import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://prso.czech.radio/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Prague Radio Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|'
    'October|November|December'
)
DATE_RE = re.compile(
    rf'(?P<date>(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*'
    rf'(?:\d{{1,2}}\s+(?:{MONTHS})|(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?)'
    rf'(?:,)?\s+20\d{{2}})',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\bat\s+(?P<hour>\d{1,2})(?:[.:](?P<minute>\d{2}))?\s*'
    r'(?P<period>a\.?\s*m\.?|p\.?\s*m\.?)?',
    re.IGNORECASE,
)

# Event pages name the building, but usually omit Prague. These are all
# Prague venues used by the orchestra's calendar and archive.
PRAGUE_VENUE_MARKERS = (
    'Rudolfinum', 'Municipal House', 'Obecn\u00ed d\u016fm', 'DOX',
    'Czech Radio', 'Studio 1', 'Studio S1', 'Forum Karl\u00edn',
    'Convent of St Agnes', 'Convent of St. Agnes', 'St Agnes Convent',
    'St. Agnes Convent', 'Ane\u017esk\u00fd kl\u00e1\u0161ter', '\u017dof\u00edn',
    'Bethlehem Chapel', 'National Museum', 'V\u00edtkov', 'Vltavsk\u00e1 filharmonie',
)

CITY_COUNTRIES = {
    'Prague': 'CZ', 'Praha': 'CZ', 'Litomy\u0161l': 'CZ', 'Zl\u00edn': 'CZ',
    'Ostrava': 'CZ', 'Brno': 'CZ', 'Pardubice': 'CZ', 'Plze\u0148': 'CZ',
    'Karlovy Vary': 'CZ', 'Česk\u00fd Krumlov': 'CZ', 'Lite\u0148': 'CZ',
    'Tokyo': 'JP', 'Osaka': 'JP', 'Nagoya': 'JP', 'Kyoto': 'JP',
    'Vienna': 'AT', 'Wien': 'AT', 'Bratislava': 'SK', 'Berlin': 'DE',
    'Hamburg': 'DE', 'Munich': 'DE', 'Dresden': 'DE', 'Warsaw': 'PL',
    'Budapest': 'HU', 'Paris': 'FR', 'London': 'GB', 'Lucerne': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def sitemap_urls(session):
    index = get_soup(session, SITEMAP_URL, 'xml')
    sitemap_links = [node.get_text(strip=True) for node in index.select('sitemap > loc')]
    if not sitemap_links:
        sitemap_links = [SITEMAP_URL]

    urls = []
    for sitemap_url in sitemap_links:
        sitemap = get_soup(session, sitemap_url, 'xml')
        urls.extend(node.get_text(strip=True) for node in sitemap.select('url > loc'))
    return list(dict.fromkeys(url for url in urls if url.startswith(SOURCE_URL)))


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group('hour'))
    minute = int(match.group('minute') or 0)
    period = re.sub(r'[^ap]', '', (match.group('period') or '').lower())
    if period == 'p' and hour < 12:
        hour += 12
    elif period == 'a' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def parse_location(perex):
    match = DATE_RE.search(perex)
    if not match:
        return None
    venue = clean_text(perex[:match.start()].rstrip(' ,\u2013-'))
    if not venue or len(venue) > 160:
        return None

    city = None
    country_code = None
    location_text = venue
    for candidate, code in CITY_COUNTRIES.items():
        if re.search(rf'\b{re.escape(candidate)}\b', location_text, re.IGNORECASE):
            city, country_code = candidate, code
            break
    if not city and any(marker.casefold() in venue.casefold() for marker in PRAGUE_VENUE_MARKERS):
        city, country_code = 'Prague', 'CZ'
    if not city:
        return None

    # When the location is "building, city", retain only the building as venue.
    parts = [part.strip() for part in venue.split(',') if part.strip()]
    if len(parts) > 1 and any(city.casefold() == part.casefold() for part in parts[1:]):
        venue = parts[0]
    if venue.casefold() == city.casefold():
        return None
    return venue, city, country_code, match.group('date')


def parse_event(url, soup):
    title_el = soup.select_one('h1.article-type')
    perex_el = soup.select_one('.field-perex')
    if not title_el or not perex_el:
        return None
    title = clean_text(title_el.get_text(' ', strip=True))
    perex = clean_text(perex_el.get_text(' ', strip=True))
    location = parse_location(perex)
    if not title or not location:
        return None
    venue, city, country_code, date_text = location
    try:
        event_date = date_parser.parse(date_text, fuzzy=True).date().isoformat()
        datetime.strptime(event_date, '%Y-%m-%d')
    except (TypeError, ValueError, OverflowError):
        return None

    body_el = soup.select_one('.field.body')
    description = clean_text(body_el) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(perex),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


def scrape_page(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return parse_event(url, get_soup(session, url))


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = sitemap_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(scrape_page, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape PRSO page',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PrsoCzechRadioCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='prso_czech_radio',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PrsoCzechRadioCrawler().run()


if __name__ == '__main__':
    main()
