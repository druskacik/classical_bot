import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://helsinginkaupunginorkesteri.fi/fi'
CONCERTS_URL = f'{SOURCE_URL}/konsertit'
SOURCE = 'Helsingin kaupunginorkesteri'
FIRST_ARCHIVE_YEAR = 2018
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}

# The hall taxonomy normally contains only a venue name, not its municipality.
# These non-Helsinki halls are the ones used for the orchestra's touring dates.
LOCATION_TOKENS = {
    'espoo': ('Espoo', 'FI'),
    'tapiola': ('Espoo', 'FI'),
    'vantaa': ('Vantaa', 'FI'),
    'lahti': ('Lahti', 'FI'),
    'turku': ('Turku', 'FI'),
    'tampere': ('Tampere', 'FI'),
    'porvoo': ('Porvoo', 'FI'),
    'järvenpää': ('Järvenpää', 'FI'),
    'hyvinkää': ('Hyvinkää', 'FI'),
    'hämeenlinna': ('Hämeenlinna', 'FI'),
    'kuopio': ('Kuopio', 'FI'),
    'oulu': ('Oulu', 'FI'),
    'tokyo': ('Tokyo', 'JP'),
    'tōkyō': ('Tokyo', 'JP'),
    'yokohama': ('Yokohama', 'JP'),
    'seoul': ('Seoul', 'KR'),
    'soul': ('Seoul', 'KR'),
    'london': ('London', 'GB'),
    'berlin': ('Berlin', 'DE'),
    'wien': ('Vienna', 'AT'),
    'vienna': ('Vienna', 'AT'),
    'paris': ('Paris', 'FR'),
    'amsterdam': ('Amsterdam', 'NL'),
    'hamburg': ('Hamburg', 'DE'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session, year):
    soup = get_soup(
        session,
        CONCERTS_URL,
        params={
            'field_show_datetime_value': f'{year}-01-01',
            'field_show_datetime_end_value': f'{year}-12-31',
        },
    )
    return {
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('a[href*="/fi/konsertit/"]')
        if link.get('href')
    }


def all_concert_urls(session):
    # The Drupal archive has no pagination, but broad multi-year ranges can time
    # out. One request per year is both reliable and covers every retained date.
    years = range(FIRST_ARCHIVE_YEAR, date.today().year + 3)
    urls = set()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(listing_urls, session, year): year for year in years}
        for future in as_completed(futures):
            year = futures[future]
            try:
                urls.update(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape HKO concert archive year',
                    event='crawler_item_failed',
                    level='warning',
                    url=CONCERTS_URL,
                    archive_year=year,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(urls)


def parse_datetime(soup):
    value = clean_text(soup.select_one('.group__right .sidebar-date'))
    time_value = clean_text(soup.select_one('.group__right .sidebar-time'))
    match = re.search(r'(\d{2})/(\d{2})/(\d{4})', value)
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(0), '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', time_value)
    return event_date, time_match.group(0) if time_match else None


def resolve_location(title, venue):
    searchable = f'{venue} {title}'.casefold()
    for token, location in LOCATION_TOKENS.items():
        if token in searchable:
            return location

    # Concerts without an explicit touring location are the institution's
    # Helsinki performances. Avoid applying that default to a stated tour.
    if not re.search(r'kiertue|tour|vierailu', title, re.IGNORECASE):
        return 'Helsinki', 'FI'
    return None, None


def programme_text(soup):
    works = []
    container = soup.select_one('.programme-container')
    if not container:
        return ''
    for piece in container.select('.node--type-musical-piece'):
        composer = clean_text(piece.select_one('.field-composer'))
        work = clean_text(piece.select_one('.field-opus'))
        value = f'{composer}: {work}' if composer and work else composer or work
        if value and value not in works:
            works.append(value)
    return 'Ohjelma\n' + '\n'.join(works) if works else ''


def parse_concert(soup, url):
    title = clean_text(soup.select_one('h1.node__title'))
    event_date, time_from = parse_datetime(soup)
    venue = clean_text(soup.select_one('.field-hall .taxonomy-term-title'))
    city, country_code = resolve_location(title, venue)
    body = clean_text(soup.select_one('.introduction-container .body'))
    programme = programme_text(soup)
    description = '\n\n'.join(part for part in (body, programme) if part) or None

    if not all((title, event_date, url, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = all_concert_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_concert(future.result(), url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape HKO concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class HelsinginKaupunginorkesteriFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='helsinginkaupunginorkesteri_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
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
    HelsinginKaupunginorkesteriFiCrawler().run()


if __name__ == '__main__':
    main()
