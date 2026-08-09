import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bayreuther-festspiele.de/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'fsdb/spielplaene/')
SOURCE = 'Bayreuther Festspiele'
CITY = 'Bayreuth'
DEFAULT_VENUE = 'Bayreuther Festspielhaus'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The archive does not carry a venue field. These are the explicitly named
# satellite events published by the festival; its regular staged productions
# and concerts take place in the Festspielhaus.
SPECIAL_VENUES = {
    'festspiel open air': 'Festspielpark Bayreuth',
    'chor-open air': 'Festspielpark Bayreuth',
    'catherine foster and friends': 'Markgräfliches Opernhaus Bayreuth',
    'venus, engel & die nacht': 'Parkhaus Oberfrankenhalle',
    'brünnhilde brennt': 'Friedrichsforum Bayreuth',
}
UNCERTAIN_VENUE_TITLES = ('diskurs', 'atmen/lauschen', 'fabrik präsentiert')


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+((?:19|20)\d{2})'
        r'(?:,\s*(\d{1,2}):(\d{2})\s*Uhr)?',
        value,
    )
    if not match:
        return None, None
    months = {
        'januar': 1, 'februar': 2, 'märz': 3, 'april': 4, 'mai': 5,
        'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
        'oktober': 10, 'november': 11, 'dezember': 12,
    }
    month = months.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), month, int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(4):
        event_time = f'{int(match.group(4)):02d}:{match.group(5)}'
    return event_date, event_time


def resolve_venue(title, info):
    combined = f'{title}\n{info}'.lower()
    explicit = (
        ('markgräflichen opernhaus', 'Markgräfliches Opernhaus Bayreuth'),
        ('parkhaus oberfrankenhalle', 'Parkhaus Oberfrankenhalle'),
        ('friedrichsforum', 'Friedrichsforum Bayreuth'),
        ('probebühne 4', 'Probebühne 4 der Bayreuther Festspiele'),
    )
    for marker, venue in explicit:
        if marker in combined:
            return venue
    normalized_title = title.casefold()
    for marker, venue in SPECIAL_VENUES.items():
        if marker in normalized_title:
            return venue
    if any(marker in normalized_title for marker in UNCERTAIN_VENUE_TITLES):
        return None
    return DEFAULT_VENUE


def available_years(session):
    soup = get_soup(session, ARCHIVE_URL)
    years = set()
    for link in soup.select('.fsdb__spielplaene--link a[href]'):
        match = re.fullmatch(r'(20\d{2}|19\d{2})', clean_text(link))
        if match:
            years.add(int(match.group(1)))
    if not years:
        raise ValueError('No performance archive years found')
    return sorted(years)


def parse_year_page(soup):
    records = []
    for item in soup.select('li.fsdb__performances--item'):
        title_node = item.select_one('.fsdb__performances--title')
        link = title_node.select_one('a[href]') if title_node else None
        datetime_text = clean_text(item.select_one('.fsdb__performances--datetime'))
        info = clean_text(item.select_one('.fsdb__performances--info'))
        title = clean_text(title_node)
        event_date, event_time = parse_datetime(datetime_text)
        venue = resolve_venue(title, info)
        url = urljoin(SOURCE_URL, link.get('href', '')) if link else ''
        if not all((title, event_date, url, venue)):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': CITY,
            'country_code': 'DE',
            'description': info or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_year(session, year):
    url = urljoin(ARCHIVE_URL, f'{year}/')
    return parse_year_page(get_soup(session, url))


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry, pool_maxsize=4))
    years = available_years(session)
    records = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(scrape_year, session, year): year for year in years}
        for future in as_completed(futures):
            year = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Bayreuther Festspiele archive year',
                    event='crawler_page_failed',
                    level='warning',
                    url=urljoin(ARCHIVE_URL, f'{year}/'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class BayreutherFestspieleDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bayreuther_festspiele_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BayreutherFestspieleDeCrawler().run()


if __name__ == '__main__':
    main()
