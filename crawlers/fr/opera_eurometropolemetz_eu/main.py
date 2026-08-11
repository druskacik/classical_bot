import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera.eurometropolemetz.eu/'
SOURCE = "Opéra-Théâtre de l'Eurométropole de Metz"
LISTING_URLS = (
    urljoin(SOURCE_URL, 'fr/a-l-affiche.html'),
    urljoin(SOURCE_URL, 'fr/archives-des-saisons.html'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
MONTHS = {
    'janv': 1, 'janvier': 1, 'fevr': 2, 'fevrier': 2, 'mars': 3,
    'avr': 4, 'avril': 4, 'mai': 5, 'juin': 6, 'juil': 7,
    'juillet': 7, 'aout': 8, 'sept': 9, 'septembre': 9, 'oct': 10,
    'octobre': 10, 'nov': 11, 'novembre': 11, 'dec': 12, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def folded(value):
    value = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in value if not unicodedata.combining(char))


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    return session


def occurrences(value):
    text = folded(value)
    pattern = re.compile(
        r'\b(\d{1,2})\s+([a-z]+)\.?\s+(20\d{2})\s*'
        r'(?:/|de)?\s*(?:a\s*)?([01]?\d|2[0-3])\s*h\s*([0-5]\d)?',
    )
    found = []
    for match in pattern.finditer(text):
        month = MONTHS.get(match.group(2).rstrip('.'))
        if not month:
            continue
        try:
            event_date = date(int(match.group(3)), month, int(match.group(1)))
        except ValueError:
            continue
        found.append((event_date.isoformat(), f'{int(match.group(4)):02d}:{int(match.group(5) or 0):02d}'))
    return found


def location_data(value):
    raw = clean_text(value)
    if not raw:
        return None, None
    normalized = folded(raw)
    if 'marly' in normalized or re.search(r'\b57155\b', raw):
        city = 'Marly'
    elif 'metz' in normalized or re.search(r'\b57(?:000|070)\b', raw):
        city = 'Metz'
    else:
        return None, None

    venue = re.sub(r"^(?:à l['’]|a l['’]|à la|a la|aux|au|à|a)\s*", '', raw, flags=re.I)
    venue = re.split(r'\s+-\s+(?:Metz|Marly)\s*$', venue, flags=re.I)[0]
    venue = re.split(r'\s+\d{1,3}(?:[,.]?\s+|\s+)(?:rue|place|avenue|boulevard)\b', venue, flags=re.I)[0]
    venue = re.split(r'\s+57\d{3}\b', venue)[0]
    venue = clean_text(venue)
    if not venue or folded(venue) == folded(city):
        return None, None
    return venue, city


def listing_items(session, listing_url):
    response = session.get(listing_url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    items = []
    for article in soup.select('article[data-goto-url]'):
        title = clean_text(article.select_one('.media-heading'))
        date_node = article.select_one('.date')
        place_node = article.select_one('.lieu')
        url = urljoin(listing_url, article.get('data-goto-url', ''))
        dates = occurrences(date_node)
        venue, city = location_data(place_node)
        if title and url and dates and venue and city:
            items.append((title, url, dates, venue, city))
    return items


def detail_description(url):
    session = make_session()
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    body = soup.select_one('#mainSection .tpl_fiche') or soup.select_one('#mainSection')
    if not body:
        return None
    for node in body.select('script, style, .date, .lieu, .btns, h1'):
        node.decompose()
    text = clean_text(body)
    return text or None


class OperaEurometropoleMetzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_eurometropolemetz_eu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        session = make_session()
        items = []
        for listing_url in LISTING_URLS:
            try:
                items.extend(listing_items(session, listing_url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event listing',
                    event='crawler_page_failed',
                    level='warning',
                    url=listing_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        descriptions = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(detail_description, item[1]): item[1] for item in items}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    descriptions[url] = None
                    log_message(
                        'Failed to scrape event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for title, url, dates, venue, city in items:
            for event_date, time_from in dates:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'FR',
                    'description': descriptions.get(url),
                })
        return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


def main():
    return OperaEurometropoleMetzCrawler().run()


if __name__ == '__main__':
    main()
