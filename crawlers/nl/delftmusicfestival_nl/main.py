import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://delftmusicfestival.nl/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programma?_locale=nl')
SOURCE = 'Delft Chamber Music Festival'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}
MONTHS = {
    'januari': 1,
    'februari': 2,
    'maart': 3,
    'april': 4,
    'mei': 5,
    'juni': 6,
    'juli': 7,
    'augustus': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'december': 12,
}
LOCATION_CITIES = {
    '/locatie/theater-de-veste': 'Delft',
    '/locatie/op-hodenpijl': 'Schipluiden',
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
    return BeautifulSoup(response.text, 'html.parser')


def programme_links(session):
    soup = get_soup(session, PROGRAMME_URL)
    links = set()
    for anchor in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, anchor.get('href'))
        path = urlparse(url).path.rstrip('/')
        if re.fullmatch(r'/programma/[^/]+', path):
            links.add(url)
    return sorted(links)


def parse_date(value):
    match = re.search(r'(\d{1,2})\s+([a-z]+)\s+(\d{4})', value.lower())
    if not match:
        return None
    day, month_name, year = match.groups()
    month = MONTHS.get(month_name)
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day)).date().isoformat()
    except ValueError:
        return None


def parse_detail(session, url):
    soup = get_soup(session, url)
    article = soup.select_one('article.item')
    if not article:
        return None

    title = clean_text(article.select_one('h1.title'))
    date_value = parse_date(clean_text(article.select_one('header.pageheader h2')))
    header = article.select_one('header.pageheader')
    header_text = clean_text(header)
    time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', header_text)
    time_from = time_match.group(0).zfill(5) if time_match else None

    location = article.select_one('header.pageheader a[href^="/locatie/"]')
    venue = clean_text(' '.join(location.find_all(string=True, recursive=False))) if location else ''
    location_path = urlparse(location.get('href')).path.rstrip('/') if location else ''
    city = LOCATION_CITIES.get(location_path)

    # The cycling-route entry names only a region and several unspecified stops,
    # so it is intentionally skipped rather than inventing one venue and city.
    if not title or not date_value or not venue or not city:
        return None

    description_node = article.select_one('.medium-8.cell')
    description = clean_text(description_node) or None
    return {
        'title': title,
        'date': date_value,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'NL',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = programme_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_detail, session, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class DelftMusicFestivalNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='delftmusicfestival_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
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
    DelftMusicFestivalNlCrawler().run()


if __name__ == '__main__':
    main()
