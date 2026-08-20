import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lamonnaiedemunt.be/fr'
PROGRAM_URL = f'{SOURCE_URL}/program'
SOURCE = 'La Monnaie / De Munt'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-BE,fr;q=0.9,en;q=0.7',
}
MONTHS = {
    'janv': 1, 'fév': 2, 'mars': 3, 'avr': 4, 'mai': 5, 'juin': 6,
    'juil': 7, 'août': 8, 'sept': 9, 'oct': 10, 'nov': 11, 'déc': 12,
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
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry, pool_maxsize=6))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    soup = get_soup(session, PROGRAM_URL)
    urls = set()
    # grid-prod contains the whole published season. The separate list-table on
    # this page is the site's "Plus tôt cette saison" archive.
    for link in soup.select('.grid-prod a[href*="/program/"], main .list-table a[href*="/program/"]'):
        url = urljoin(PROGRAM_URL, link.get('href', '')).split('#', 1)[0]
        path = urlparse(url).path
        if re.search(r'/fr/program/\d+', path):
            urls.add(url)
    return sorted(urls)


def parse_french_date(value):
    normalized = clean_text(value).lower().replace('.', '')
    match = re.search(r'\b(\d{1,2})\s+([a-zà-ÿ]+)\s+(\d{4})\b', normalized)
    if not match:
        return None
    day, month_name, year = match.groups()
    month = MONTHS.get(month_name)
    if not month:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def event_description(soup):
    parts = []
    for section in soup.select('.text--activity, .credits .container'):
        text = clean_text(section)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def event_location(venue):
    venue = clean_text(venue)
    if not venue:
        return None
    city_match = re.search(r'\(([^()]+)\)\s*$', venue)
    if city_match:
        city = city_match.group(1).strip()
        country_code = 'FR' if city.casefold() == 'colmar' else None
        if country_code:
            return venue, city, country_code
        return None
    # Every published location without an explicit foreign city is one of La
    # Monnaie's Brussels buildings or a named Brussels partner venue.
    return venue, 'Brussels', 'BE'


def parse_detail(url, soup):
    title = re.sub(r'\s+', ' ', clean_text(soup.select_one('h1 .prod-title'))).strip()
    if not title:
        return []
    description = event_description(soup)
    records = []
    for performance in soup.select('.agenda .list-table > li'):
        heading = performance.select_one('h4')
        event_date = parse_french_date(heading)
        location = event_location(performance.select_one('.td-grow p'))
        if not event_date or not location:
            continue
        venue, city, country_code = location
        time_text = clean_text(performance.select_one('.td-hour p'))
        time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', time_text)
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
        records.append({
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
        })
    return records


class LaMonnaieDeMuntBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lamonnaiedemunt_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        urls = listing_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(get_soup, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(url, future.result()))
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape programme detail',
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
    LaMonnaieDeMuntBeCrawler().run()


if __name__ == '__main__':
    main()
