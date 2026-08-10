import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rsh-duesseldorf.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'veranstaltungen')
SOURCE = 'Robert Schumann Hochschule Düsseldorf'
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
MONTHS = {
    'Januar': 1, 'Februar': 2, 'März': 3, 'April': 4,
    'Mai': 5, 'Juni': 6, 'Juli': 7, 'August': 8,
    'September': 9, 'Oktober': 10, 'November': 11, 'Dezember': 12,
}
CITY_MARKERS = {
    'wuppertal': 'Wuppertal',
    'düsseldorf': 'Düsseldorf',
    'duesseldorf': 'Düsseldorf',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\xad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
    )))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def event_urls(soup):
    return sorted({
        canonical_url(link['href'])
        for link in soup.select('a[href*="/veranstaltungen/details/"]')
    })


def parse_date(value):
    text = clean_text(value)
    match = re.search(r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(20\d{2})', text)
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


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])(?::([0-5]\d))?', clean_text(value))
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def infer_city(venue):
    folded = venue.casefold()
    for marker, city in CITY_MARKERS.items():
        if marker in folded:
            return city
    # The calendar belongs to a Düsseldorf music institution and its unqualified
    # halls (Partika-Saal, Robert-Schumann-Saal, Campus, etc.) are local venues.
    return 'Düsseldorf'


def parse_detail(soup, url):
    event = soup.select_one('.event-single')
    if not event:
        return None
    title = clean_text(event.select_one('h2'))
    event_date = parse_date(event.select_one('.date'))
    venue = clean_text(event.select_one('.location'))
    if not title or not event_date or not venue:
        return None

    content = event.select_one('.col-sm-8')
    description_parts = []
    if content:
        for node in content.select(':scope > p, :scope > .text'):
            value = clean_text(node)
            if value and value not in description_parts:
                description_parts.append(value)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event.select_one('.time')),
        'venue': venue,
        'city': infer_city(venue),
        'country_code': 'DE',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = make_session()
    urls = event_urls(get_soup(session, CALENDAR_URL))
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_detail(future.result(), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape RSH Düsseldorf event detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url'],
    ))


class RshDuesseldorfDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rsh_duesseldorf_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    RshDuesseldorfDeCrawler().run()


if __name__ == '__main__':
    main()
