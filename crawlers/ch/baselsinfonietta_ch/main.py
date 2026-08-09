import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.baselsinfonietta.ch/'
CONCERTS_URL = urljoin(SOURCE_URL, 'konzerte')
ARCHIVE_URL = urljoin(SOURCE_URL, 'archiv')
SOURCE = 'Basel Sinfonietta'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'dezember': 12,
}

# The calendar has no separate city field. These venue fragments cover its
# home venues and the explicitly identified touring venues in the archive.
LOCATIONS = {
    'basel': ('Basel', 'CH'),
    'reigoldswil': ('Reigoldswil', 'CH'),
    'bern': ('Bern', 'CH'),
    'zürich': ('Zürich', 'CH'),
    'genf': ('Genf', 'CH'),
    'geneve': ('Genf', 'CH'),
    'saint-louis': ('Saint-Louis', 'FR'),
    'lörrach': ('Lörrach', 'DE'),
    'witten': ('Witten', 'DE'),
    'köln': ('Köln', 'DE'),
    'kölner': ('Köln', 'DE'),
    'essen': ('Essen', 'DE'),
    'hamburg': ('Hamburg', 'DE'),
    'bochum': ('Bochum', 'DE'),
    'kopenhagen': ('Kopenhagen', 'DK'),
    'warschau': ('Warschau', 'PL'),
    'zagreb': ('Zagreb', 'HR'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    urls = set()
    for page_url in (CONCERTS_URL, ARCHIVE_URL):
        start = 0
        while True:
            url = page_url if not start else f'{page_url}?start={start}'
            soup = get_soup(session, url)
            found = {
                urljoin(url, link.get('href'))
                for link in soup.select('.article .readmore a[href]')
            }
            new_urls = found - urls
            urls.update(found)
            if page_url == CONCERTS_URL or not found or not new_urls:
                break
            start += 12
    return sorted(urls)


def parse_location(venue):
    folded = venue.casefold()
    for fragment, location in LOCATIONS.items():
        if fragment in folded:
            return location
    return None, None


def parse_date_times(value):
    text = clean_text(value).casefold()
    long_match = re.search(
        r'(\d{1,2})\.\s*([a-zä]+)\s*(\d{4})', text,
    )
    short_match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', text)
    try:
        if long_match:
            day, month_name, year = long_match.groups()
            event_date = date(int(year), MONTHS[month_name], int(day))
        elif short_match:
            day, month, year = short_match.groups()
            year = int(year) + (2000 if len(year) == 2 else 0)
            event_date = date(year, int(month), int(day))
        else:
            return []
    except (KeyError, ValueError):
        return []

    tail = text[(long_match or short_match).end():]
    if 'einlass' in tail:
        return [(event_date.isoformat(), None)]
    times = re.findall(r'(?<!\d)([0-2]?\d)[.:]([0-5]\d)(?!\d)', tail)
    if not times:
        # Forms such as "19 Uhr" are common on the current calendar.
        times = re.findall(r'(?<!\d)([0-2]?\d)\s*uhr', tail)
        times = [(hour, '00') for hour in times]
    return [(event_date.isoformat(), f'{int(hour):02d}:{minute}') for hour, minute in times] or [
        (event_date.isoformat(), None)
    ]


def parse_detail(soup, url):
    main = soup.select_one('main')
    body = soup.select_one('[itemprop="articleBody"]')
    if not main or not body:
        return []
    title_node = main.select_one('h1')
    if clean_text(title_node).casefold() == 'konzertkalender':
        title_node = body.select_one('h2')
    title = ' – '.join(clean_text(title_node).splitlines())
    venue_node = main.select_one('.konzert-venue')
    venue = clean_text(venue_node.select_one('.field-value') if venue_node else None)
    city, country_code = parse_location(venue)
    if not all((title, venue, city, country_code)):
        return []

    description = clean_text(body) or None
    performances = []
    date_nodes = [
        node for node in main.select('.konzert-date .field-value')
        if not node.find_parent('article', class_='mod-articles-item')
    ]
    for date_node in date_nodes:
        performances.extend(parse_date_times(date_node))

    return [
        {
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
        }
        for event_date, time_from in performances
    ]


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_detail(future.result(), url))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Basel Sinfonietta concert',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class BaselSinfoniettaChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='baselsinfonietta_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BaselSinfoniettaChCrawler().run()


if __name__ == '__main__':
    main()
