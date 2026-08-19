import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.pfz.hu/'
SOURCE = 'Pannon Filharmonikusok'
LIST_URL = urljoin(SOURCE_URL, 'elo-koncertek')
ARCHIVE_URL = 'http://old.pfz.hu/archivum/korabbi-hangversenyek'
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0 (+https://www.pfz.hu/)',
    'Accept-Language': 'hu-HU,hu;q=0.9,en;q=0.7',
}

MONTHS = {
    'január': 1, 'jan': 1, 'február': 2, 'feb': 2, 'március': 3,
    'márc': 3, 'már': 3, 'április': 4, 'ápr': 4, 'május': 5,
    'máj': 5, 'június': 6, 'jún': 6, 'július': 7, 'júl': 7,
    'augusztus': 8, 'aug': 8, 'szeptember': 9, 'szept': 9,
    'október': 10, 'okt': 10, 'november': 11, 'nov': 11,
    'december': 12, 'dec': 12,
}

# Venues for which the publisher gives enough context to infer a locality.
# Explicit tour locations below take precedence over these home-venue defaults.
VENUE_LOCATIONS = {
    'kodály központ': ('Pécs', 'HU'),
    'pécsi bazilika': ('Pécs', 'HU'),
    'bazilika pécs': ('Pécs', 'HU'),
    'müpa': ('Budapest', 'HU'),
    'művészetek palotája': ('Budapest', 'HU'),
    'musikverein': ('Vienna', 'AT'),
}
COUNTRY_MARKERS = {
    'ausztria': 'AT', 'austria': 'AT', 'németország': 'DE', 'germany': 'DE',
    'horvátország': 'HR', 'croatia': 'HR', 'szlovákia': 'SK',
    'slovakia': 'SK', 'románia': 'RO', 'romania': 'RO',
}
CITY_ALIASES = {
    'bécs': 'Vienna', 'vienna': 'Vienna', 'budapest': 'Budapest',
    'pécs': 'Pécs', 'zágráb': 'Zagreb', 'zagreb': 'Zagreb',
    'pozsony': 'Bratislava', 'bratislava': 'Bratislava',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def parse_hungarian_date(value):
    match = re.search(r'(20\d{2})\.\s*([a-záéíóöőúüű]+)\.?\s*(\d{1,2})\.?', value.casefold())
    if not match:
        return None
    month = MONTHS.get(match.group(2).rstrip('.'))
    if not month:
        return None
    try:
        return date(int(match.group(1)), month, int(match.group(3))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])[.:]([0-5]\d)(?!\d)', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_location(venue):
    folded = venue.casefold()
    country_code = 'HU'
    for marker, code in COUNTRY_MARKERS.items():
        if marker in folded:
            country_code = code
            break
    for marker, city in CITY_ALIASES.items():
        if re.search(rf'\b{re.escape(marker)}\b', folded):
            return city, country_code
    for marker, location in VENUE_LOCATIONS.items():
        if marker in folded:
            return location
    return None


def current_urls(session):
    urls = set()
    page = 1
    while True:
        soup = get_soup(session, LIST_URL, params={'page': page})
        page_urls = {
            urljoin(SOURCE_URL, node['href'])
            for node in soup.select('a[href*="/koncert/"]')
        }
        new_urls = page_urls - urls
        urls.update(page_urls)
        next_link = soup.select_one(f'a[href*="page={page + 1}"]')
        if not new_urls or not next_link:
            break
        page += 1
    return urls


def archive_urls(session):
    first = get_soup(session, ARCHIVE_URL)
    pages = [1]
    pages.extend(
        int(match.group(1))
        for node in first.select('a[href*="oldal:"]')
        if (match := re.search(r'oldal:(\d+)', node.get('href', '')))
    )
    last_page = max(pages)
    soups = [first]
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(get_soup, session, f'{ARCHIVE_URL}/oldal:{page}')
            for page in range(2, last_page + 1)
        ]
        for future in as_completed(futures):
            soups.append(future.result())

    urls = set()
    for soup in soups:
        urls.update(
            urljoin(ARCHIVE_URL, node['href'])
            for node in soup.select('a[href*="/rendezvenyeink/koncert/"]')
        )
    return urls


def parse_current(session, url):
    soup = get_soup(session, url)
    title_node = soup.select_one('h1.koncert-cim')
    if title_node:
        for series in title_node.select('span'):
            series.decompose()
    title = clean_text(title_node)
    info = soup.select_one('time.text-uppercase')
    info_text = clean_text(info)
    event_date = parse_hungarian_date(info_text)
    time_from = parse_time(info_text)
    venue = clean_text(soup.select_one('.koncert-helyszin'))
    location = parse_location(venue)
    if not all((title, event_date, venue, location)):
        return None

    description_parts = []
    for selector in ('.koncert-musor', '.koncert-szovegek'):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)
    city, country_code = location
    return {
        'title': title, 'date': event_date, 'url': url,
        'time_from': time_from, 'venue': venue, 'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL, 'source': SOURCE,
    }


def parse_archive(session, url):
    soup = get_soup(session, url)
    root = soup.select_one('.koncertadatlap')
    if not root:
        return None
    title = clean_text(root.select_one('h2'))
    info = root.select_one('.kocertadatlap_alcim')
    info_text = clean_text(info)
    event_date = parse_hungarian_date(info_text)
    time_from = parse_time(info_text)
    venue = ''
    if info:
        venue = clean_text(info)
        date_node = info.select_one('strong')
        if date_node:
            venue = venue.replace(clean_text(date_node), '', 1)
        venue = re.sub(r'^\s*\d{1,2}[.:]\d{2}\s*\|?\s*', '', venue).strip(' |')
    location = parse_location(venue)
    if not all((title, event_date, venue, location)):
        return None

    description_parts = []
    programme = clean_text(root.select_one('.koncertadatlap_musor'))
    if programme:
        description_parts.append(programme)
    for box in root.select('.koncertadatlap_szoveges_doboz'):
        heading = clean_text(box.select_one('h3'))
        if heading.casefold() == 'a műsorról':
            text = clean_text(box)
            if text:
                description_parts.append(text)
    city, country_code = location
    return {
        'title': title, 'date': event_date, 'url': url,
        'time_from': time_from, 'venue': venue, 'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL, 'source': SOURCE,
    }


class PfzHuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pfz_hu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HU',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        jobs = []
        for finder, parser, label in (
            (current_urls, parse_current, 'current'),
            (archive_urls, parse_archive, 'archive'),
        ):
            try:
                jobs.extend((parser, url) for url in finder(session))
            except requests.RequestException as error:
                log_message(
                    'Failed to enumerate PFZ concert feed',
                    event='crawler_page_failed', level='warning', url=LIST_URL,
                    feed=label, error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(parser, session, url): url
                for parser, url in jobs
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape PFZ concert',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    PfzHuCrawler().run()


if __name__ == '__main__':
    main()
