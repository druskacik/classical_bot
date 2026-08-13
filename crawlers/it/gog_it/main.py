import re
from datetime import datetime
from urllib.parse import unquote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gog.it/'
SOURCE = 'GOG - Giovine Orchestra Genovese'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}
MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def page_links(soup, fragment):
    links = []
    for link in soup.select(f'a[href*="/{fragment}/"]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if urlparse(url).netloc == 'www.gog.it' and url not in links:
            links.append(url)
    return links


def discover_event_urls(session):
    queue = [SOURCE_URL]
    visited = set()
    events = []
    event_ids = set()
    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        soup = get_soup(session, url)
        for event_url in page_links(soup, 'spettacoli'):
            match = re.search(r'/spettacoli/(\d+)-', event_url)
            event_id = match.group(1) if match else event_url
            if event_id not in event_ids:
                event_ids.add(event_id)
                events.append(event_url)
        for series_url in page_links(soup, 'rassegne'):
            if series_url not in visited and series_url not in queue:
                queue.append(series_url)
    return events


def parse_date_time(value):
    match = re.search(
        r'\b(\d{1,2})\s+([a-zà]+)\s+(\d{4})(?:\s+ore\s+(\d{1,2})[.:](\d{2}))?',
        value.casefold(),
    )
    if not match:
        return None
    try:
        parsed = datetime(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        )
    except (KeyError, ValueError):
        return None
    time_from = None
    if match.group(4) and 0 <= int(match.group(4)) <= 23:
        time_from = f'{int(match.group(4)):02d}:{match.group(5)}'
    return parsed.date().isoformat(), time_from


def location(soup):
    node = soup.select_one('.Luogo')
    venue = clean_text(node)
    link = node.select_one('a[href]') if node else None
    if not venue or not link:
        return None

    map_path = urlparse(link.get('href', '')).path.rstrip('/')
    map_text = unquote_plus(map_path.rsplit('/', 1)[-1]).strip()
    # GOG's map URLs are generated as "venue address city". Most addresses have
    # a street number, which gives an unambiguous boundary before the city.
    numbered = re.search(r'\d+[A-Za-z/]?\s+(.+)$', map_text)
    city = numbered.group(1).strip(' ,') if numbered else None
    if map_text.casefold().endswith(' genova'):
        city = 'Genova'
    if not city or city.casefold() in {'italia', 'italy'}:
        return None
    return venue, city


def parse_event(soup, url):
    title = clean_text(soup.select_one('h1.Titolo'))
    parsed_date = parse_date_time(clean_text(soup.select_one('.Data')))
    parsed_location = location(soup)
    if not title or not parsed_date or not parsed_location:
        return None

    description_parts = [clean_text(node) for node in soup.select('.Testo')]
    description = clean_text('\n\n'.join(part for part in description_parts if part)) or None
    event_date, time_from = parsed_date
    venue, city = parsed_location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class GogItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gog_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            event_urls = discover_event_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to discover GOG concerts',
                event='crawler_fetch_failed', level='error', url=SOURCE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        for url in event_urls:
            try:
                record = parse_event(get_soup(session, url), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch GOG concert',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    GogItCrawler().run()


if __name__ == '__main__':
    main()
