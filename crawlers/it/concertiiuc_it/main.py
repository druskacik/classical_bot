import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.concertiiuc.it/'
SOURCE = 'IUC - Istituzione Universitaria dei Concerti'
LIST_URLS = (
    urljoin(SOURCE_URL, 'tutti-i-concerti/'),
    urljoin(SOURCE_URL, 'archivio-concerti/'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}
MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
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


def parse_date(value):
    match = re.search(r'([A-Za-zÀ-ÿ]+)\s+(\d{1,2}),\s*(\d{4})', value)
    if not match:
        return None
    month = MONTHS.get(match.group(1).casefold())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_city(location_node):
    if location_node is None:
        return None
    lines = [clean_text(line) for line in location_node.stripped_strings]
    address = next((line for line in lines if re.search(r',\s*Roma(?:,|$)', line, re.I)), '')
    if address:
        return 'Roma'
    text = clean_text(location_node)
    match = re.search(r',\s*([^,\n]+),\s*[A-Z]{2},\s*\d{5}\b', text)
    return clean_text(match.group(1)) if match else None


def parse_detail(soup, url):
    event = soup.select_one('.em-event-single')
    title_node = soup.select_one('h1')
    date_node = soup.select_one('.em-event-date')
    location_node = soup.select_one('.em-item-meta-line.em-event-location')
    venue_node = location_node.select_one('a') if location_node else None
    if not all((event, title_node, date_node, location_node, venue_node)):
        return None

    title = clean_text(title_node)
    event_date = parse_date(clean_text(date_node))
    venue = clean_text(venue_node)
    city = parse_city(location_node)
    if not all((title, event_date, venue, city)):
        return None

    time_node = soup.select_one('.em-event-time')
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(time_node))
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    description = clean_text(soup.select_one('.em-event-content')) or None
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


def event_urls(session):
    urls = []
    seen = set()
    for list_url in LIST_URLS:
        soup = get_soup(session, list_url)
        for link in soup.select('a[href*="/eventi/"]'):
            url = urljoin(SOURCE_URL, link.get('href', '')).split('#', 1)[0]
            path = urlparse(url).path.rstrip('/')
            if path == '/eventi' or url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


class ConcertiiucItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='concertiiuc_it',
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
            urls = event_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch IUC concert listings',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []

        def fetch(url):
            return parse_detail(get_soup(session, url), url)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, TypeError, ValueError) as error:
                    log_message(
                        'Failed to fetch or parse IUC event',
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
    ConcertiiucItCrawler().run()


if __name__ == '__main__':
    main()
