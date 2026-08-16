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


SOURCE_URL = 'https://hfm-karlsruhe.de/'
EVENTS_URL = urljoin(SOURCE_URL, 'veranstaltungen')
ARCHIVE_URL = urljoin(SOURCE_URL, 'veranstaltungen/archiv')
SOURCE = 'Hochschule für Musik Karlsruhe'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4,
    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def parse_listing_date(month_year, day_text):
    match = re.fullmatch(r"([A-Za-zÄÖÜäöüß]+)\s+'(\d{2})", month_year.strip())
    day_match = re.search(r'\d{1,2}', day_text)
    if not match or not day_match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if not month:
        return None
    try:
        return date(2000 + int(match.group(2)), month, int(day_match.group())).isoformat()
    except ValueError:
        return None


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for row in soup.select('.views-row[data-date]'):
        link = row.select_one('.views-field-title a[href]')
        day = row.select_one('.list-day')
        event_date = parse_listing_date(row.get('data-date', ''), clean_text(day))
        if link and event_date:
            items.append((canonical_url(link['href']), event_date))
    page_numbers = [
        int(match.group(1))
        for link in soup.select('a[href*="page="]')
        if (match := re.search(r'[?&]page=(\d+)', link.get('href', '')))
    ]
    return items, max(page_numbers, default=0)


def listing_page(session, base_url, page):
    response = session.get(base_url, params={'page': page}, timeout=45)
    response.raise_for_status()
    return parse_listing(response.text)[0]


def listing_items(session, base_url):
    response = session.get(base_url, timeout=45)
    response.raise_for_status()
    items, last_page = parse_listing(response.text)
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(listing_page, session, base_url, page)
            for page in range(1, last_page + 1)
        ]
        for future in as_completed(futures):
            items.extend(future.result())
    return items


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):(\d{2})\b', value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour >= 24 or minute >= 60:
        return None
    return f'{hour:02d}:{minute:02d}'


def parse_location(element):
    if element is None:
        return None
    venue_parts = []
    for child in element.contents:
        if getattr(child, 'name', None) == 'p':
            break
        value = clean_text(child)
        if value:
            venue_parts.append(value)
    venue = clean_text(' '.join(venue_parts))
    address = clean_text(element.select_one('p'))
    city_match = re.search(r'\b\d{5}\s+([^,·\n]+)', address)
    if not venue or not city_match:
        return None
    city = city_match.group(1).strip()
    if not city:
        return None
    return venue, city


def parse_detail(html, url, event_date):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.node--type-veranstaltung')
    if not article:
        return None
    title = clean_text(soup.select_one('h1 .field--name-title'))
    datetime_text = clean_text(article.select_one('.field--name-field-datum-veranstaltung'))
    location = parse_location(article.select_one('.field--name-field-ort'))
    if not title or not event_date or not location:
        return None
    venue, city = location

    description_parts = []
    headline = clean_text(article.select_one('.field--name-field-headline'))
    if headline:
        description_parts.append(headline)
    for body in article.select('.field--name-field-body'):
        if body.find_parent(class_='field--name-field-column-left-veranstaltung'):
            continue
        value = clean_text(body)
        if value and value not in description_parts:
            description_parts.append(value)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(datetime_text),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HfmKarlsruheDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfm_karlsruhe_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )))
        discovered = listing_items(session, EVENTS_URL) + listing_items(session, ARCHIVE_URL)
        items = sorted(set(discovered))
        records = []

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(session.get, url, timeout=45): (url, event_date)
                for url, event_date in items
            }
            for future in as_completed(futures):
                url, event_date = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_detail(response.text, url, event_date)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch HfM Karlsruhe event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ))


def main():
    HfmKarlsruheDeCrawler().run()


if __name__ == '__main__':
    main()
