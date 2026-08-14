import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lso.lv/'
SOURCE = 'Liepājas Simfoniskais orķestris'
CALENDAR_URL = urljoin(SOURCE_URL, 'afisa')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'lv-LV,lv;q=0.9,en;q=0.7',
}

# The orchestra tours extensively. These venue fragments cover the places in
# the site's published 2021-present archive; unknown locations are skipped.
LOCATION_FRAGMENTS = (
    ('lietuv', None, 'LT'),
    ('marijampol', 'Marijampolė', 'LT'),
    ('jodkrant', 'Juodkrantė', 'LT'),
    ('kuršēn', 'Kuršėnai', 'LT'),
    ('viļņ', 'Vilnius', 'LT'),
    ('mikkeli', 'Mikkeli', 'FI'),
    ('lielais dzintars', 'Liepāja', 'LV'),
    ('pūt, vējiņi', 'Liepāja', 'LV'),
    ('liepājas ', 'Liepāja', 'LV'),
    ('rundāles pil', 'Pilsrundāle', 'LV'),
    ('dzintaru koncertzāl', 'Jūrmala', 'LV'),
    ('jūrmala', 'Jūrmala', 'LV'),
    ('rīga', 'Rīga', 'LV'),
    ('rīgas ', 'Rīga', 'LV'),
    ('nacionālā opera', 'Rīga', 'LV'),
    ('lielā ģilde', 'Rīga', 'LV'),
    ('cēs', 'Cēsis', 'LV'),
    ('ventspil', 'Ventspils', 'LV'),
    ('jelgav', 'Jelgava', 'LV'),
    ('rēzekn', 'Rēzekne', 'LV'),
    ('daugavpil', 'Daugavpils', 'LV'),
    ('tukum', 'Tukums', 'LV'),
    ('tals', 'Talsi', 'LV'),
    ('balv', 'Balvi', 'LV'),
    ('limbaž', 'Limbaži', 'LV'),
    ('madon', 'Madona', 'LV'),
    ('auce', 'Auce', 'LV'),
    ('jelgav', 'Jelgava', 'LV'),
    ('siguld', 'Sigulda', 'LV'),
    ('ogres ', 'Ogre', 'LV'),
    ('smilten', 'Smiltene', 'LV'),
    ('ulbrok', 'Ulbroka', 'LV'),
    ('valmier', 'Valmiera', 'LV'),
    ('lielvārd', 'Lielvārde', 'LV'),
    ('grobiņ', 'Grobiņa', 'LV'),
    ('aizput', 'Aizpute', 'LV'),
    ('durbe', 'Durbe', 'LV'),
    ('durbes ', 'Durbe', 'LV'),
    ('pāvilost', 'Pāvilosta', 'LV'),
    ('priekul', 'Priekule', 'LV'),
    ('vecpil', 'Vecpils', 'LV'),
    ('cīrav', 'Cīrava', 'LV'),
    ('smaižu', 'Smaiži', 'LV'),
    ('piķeļu', 'Smaiži', 'LV'),
    ('nīcas ', 'Nīca', 'LV'),
    ('jūrmalciem', 'Jūrmalciems', 'LV'),
    ('kalēt', 'Kalēti', 'LV'),
    ('dēsel', 'Dēsele', 'LV'),
    ('embūtes ', 'Embūte', 'LV'),
    ('gramzd', 'Gramzda', 'LV'),
    ('bārt', 'Bārta', 'LV'),
    ('kazdang', 'Kazdanga', 'LV'),
    ('rucav', 'Rucava', 'LV'),
    ('apriķ', 'Apriķi', 'LV'),
    ('alsung', 'Alsunga', 'LV'),
    ('ziemup', 'Ziemupe', 'LV'),
    ('kalven', 'Kalvene', 'LV'),
    ('nīgrand', 'Nīgrande', 'LV'),
    ('griezes ', 'Grieze', 'LV'),
    ('virgas ', 'Virga', 'LV'),
    ('vaiņod', 'Vaiņode', 'LV'),
    ('vērgales ', 'Vērgale', 'LV'),
    ('dunalk', 'Dunalka', 'LV'),
    ('medzes ', 'Medze', 'LV'),
    ('gaviez', 'Gavieze', 'LV'),
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry, pool_maxsize=16))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def resolve_location(venue):
    lowered = venue.casefold()
    for fragment, city, country_code in LOCATION_FRAGMENTS:
        if fragment in lowered:
            if city is None:
                # Country-only labels must also contain a recognizable city.
                continue
            return city, country_code
    return None, None


def available_years(session):
    soup = get_soup(session, CALENDAR_URL)
    years = set()
    for link in soup.select('a[href]'):
        path = urlparse(urljoin(SOURCE_URL, link.get('href'))).path.rstrip('/')
        match = re.fullmatch(r'/afisa/(20\d{2})', path)
        if match:
            years.add(int(match.group(1)))
    return sorted(years)


def listing_records(session, year):
    url = urljoin(SOURCE_URL, f'afisa/{year}')
    soup = get_soup(session, url)
    records = []
    for row in soup.select('.koncerti-gads .row.border-bottom'):
        link = row.select_one('a.kon_nosaukums[href]')
        date_time = clean_text(row.select_one('.datums'))
        venue = clean_text(row.select_one('.vieta'))
        match = re.search(
            r'(?<!\d)(\d{2}\.\d{2}\.\d{4})(?:\s+([01]?\d|2[0-3])[:.]([0-5]\d))?',
            date_time,
        )
        if not link or not match or not venue:
            continue
        title = clean_text(link)
        combined = f'{title} {venue}'.casefold()
        if 'tiešraid' in combined or 'ieraksti' in combined:
            # The archive also contains broadcast-only and recording-session
            # rows, which are not public concert occurrences.
            continue
        try:
            event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
        except ValueError:
            continue
        city, country_code = resolve_location(venue)
        if not title or not city or not country_code:
            continue
        time_from = None
        if match.group(2):
            time_from = f'{int(match.group(2)):02d}:{match.group(3)}'
        records.append({
            'title': title,
            'date': event_date,
            'url': urljoin(SOURCE_URL, link.get('href')),
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def description_from_detail(soup):
    body = soup.select_one('.col-lg-8.pb-3 .minimize-p')
    return clean_text(body) or None


def get_concerts():
    session = make_session()
    records = []
    for year in available_years(session):
        try:
            records.extend(listing_records(session, year))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert year',
                event='crawler_item_failed',
                level='warning',
                url=urljoin(SOURCE_URL, f'afisa/{year}'),
                error_type=type(error).__name__,
                error_message=str(error),
            )

    descriptions = {}
    urls = sorted({record['url'] for record in records})
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = description_from_detail(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    for record in records:
        record['description'] = descriptions.get(record['url'])
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class LsoLvCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lso_lv',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LV',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LsoLvCrawler().run()


if __name__ == '__main__':
    main()
