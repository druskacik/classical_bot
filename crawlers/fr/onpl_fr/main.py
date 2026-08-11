import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://onpl.fr/'
SOURCE = 'Orchestre National des Pays de la Loire'
SITEMAP_URL = f'{SOURCE_URL}agenda-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def folded(value):
    text = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(character for character in text if not unicodedata.combining(character))


def build_session_date(value, last_date):
    match = re.search(r'\b(\d{1,2})\s+([a-z]+)\b', folded(value))
    if not match or match.group(2) not in MONTHS:
        return None
    day = int(match.group(1))
    month = MONTHS[match.group(2)]
    try:
        result = date(last_date.year, month, day)
        if result > last_date:
            result = date(last_date.year - 1, month, day)
    except ValueError:
        return None
    return result


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*[:h]\s*([0-5]\d)\b', clean_text(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def clean_city(value):
    # A small number of session rows append a landmark to the city field
    # (for example, "Angers, Hôtel du département"). The venue is provided
    # separately, so retain only the municipality here.
    return clean_text(value).split(',', 1)[0].strip()


def description_from_page(soup):
    parts = []
    programme = soup.select_one('.au-programme-inner')
    if programme:
        text = clean_text(programme)
        if text:
            parts.append(text)
    running_order = soup.select_one('.resume-soiree')
    if running_order:
        heading = running_order.find(['h2', 'h3'])
        if heading:
            heading.extract()
        text = clean_text(running_order)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main article h1'))
    last_date_node = soup.select_one('main article .date[datetime]')
    if not title or not last_date_node:
        return []
    try:
        last_date = date.fromisoformat(last_date_node.get('datetime', ''))
    except ValueError:
        return []

    description = description_from_page(soup)
    records = []
    for session in soup.select('main article .sessions .session'):
        event_date = build_session_date(clean_text(session.select_one('.session-date')), last_date)
        city = clean_city(session.select_one('.session-ville'))
        venue = clean_text(session.select_one('.session-lieu'))
        if not event_date or not city or not venue or folded(venue) == folded(city):
            continue
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': url,
            'time_from': parse_time(session.select_one('.session-horaires')),
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


def new_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    return session


def fetch_event(url):
    response = new_session().get(url, timeout=45)
    response.raise_for_status()
    return parse_event_page(response.text, url)


def sitemap_urls():
    response = new_session().get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return [
        clean_text(node.find('loc'))
        for node in soup.find_all('url')
        if node.find('loc') and clean_text(node.find('loc')).rstrip('/') != f'{SOURCE_URL}agenda'.rstrip('/')
    ]


def scrape_concerts():
    urls = sitemap_urls()
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_event, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch ONPL event page',
                    event='crawler_page_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    return sorted(records, key=lambda row: (
        row['date'], row['time_from'] or '', row['title'], row['city'], row['venue']
    ))


class OnplFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='onpl_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    OnplFrCrawler().run()


if __name__ == '__main__':
    main()
