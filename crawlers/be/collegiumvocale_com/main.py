import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.collegiumvocale.com/'
SEASON_URL = urljoin(SOURCE_URL, 'en/season/')
SOURCE = 'Collegium Vocale Gent'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
VENUE_CITIES = {
    'desingel': 'Antwerpen',
    'elbphilharmonie': 'Hamburg',
    'tap': 'Poitiers',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def concert_urls(session):
    soup = get_soup(session, SEASON_URL)
    urls = set()
    for link in soup.select('a[href*="/en/concert/"]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        path = urlparse(url).path
        if path.startswith('/en/concert/') and not path.endswith('/ical/'):
            urls.add(url)
    return sorted(urls)


def parse_location(node):
    if not node:
        return None
    country_node = node.select_one('small')
    country_code = clean_text(country_node).upper()
    if not re.fullmatch(r'[A-Z]{2}', country_code):
        return None
    if country_node:
        country_node.extract()
    parts = [part.strip() for part in clean_text(node).replace('\n', ' ').split('|')]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return None
    venue, city = parts[0], parts[1]
    if venue.casefold() == city.casefold():
        city = VENUE_CITIES.get(venue.casefold(), '')
    if not venue or not city:
        return None
    return venue, city, country_code


def parse_concert(url, soup):
    title = clean_text(soup.select_one('.concertdiv h1'))
    date_time = clean_text(soup.select_one('.datum_uur'))
    location = parse_location(soup.select_one('.locatie'))
    match = re.search(r'(\d{2}/\d{2}/\d{4})(?:\s*[—|-]\s*(\d{1,2}:\d{2}))?', date_time)
    if not title or not match or not location:
        return None
    try:
        event_date = datetime.strptime(match.group(1), '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None

    venue, city, country_code = location
    description_parts = []
    for selector in ('.compositiediv', '.uitvoerders'):
        text = clean_text(soup.select_one(selector))
        if text:
            description_parts.append(text)
    editorial = soup.select_one('.statusrel')
    if editorial:
        for unwanted in editorial.select('h1, .concertslider, script, style'):
            unwanted.decompose()
        text = clean_text(editorial)
        if text:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': match.group(2),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_concert(url, future.result())
                if record:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']))


class CollegiumVocaleComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='collegiumvocale_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    CollegiumVocaleComCrawler().run()


if __name__ == '__main__':
    main()
