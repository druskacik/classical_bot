import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bachakademie.de/de/'
SOURCE = 'Internationale Bachakademie Stuttgart'
BASE_URL = 'https://www.bachakademie.de'
LISTINGS = (
    ('/de/veranstaltungskalender.html', 'page_e117'),
    ('/de/veranstaltungsarchiv.html', 'page_e126'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}
FOREIGN_COUNTRIES = {
    'Paris': 'FR',
    'Basel': 'CH',
    'Gstaad': 'CH',
    'Lausanne': 'CH',
    'Luzern': 'CH',
    'Schaffhausen': 'CH',
    'Luxembourg': 'LU',
    'Wien': 'AT',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url, params=None):
    """Fetch a page, including a fallback for the site's intermittent TLS listener."""
    try:
        response = session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return response
    except requests.RequestException:
        if not url.startswith(BASE_URL):
            raise
        fallback = 'http://www.bachakademie.de' + url[len(BASE_URL):]
        response = session.get(fallback, params=params, timeout=45)
        response.raise_for_status()
        return response


def last_page(soup, parameter):
    pages = [1]
    for link in soup.select(f'a[href*="{parameter}="]'):
        match = re.search(rf'[?&]{re.escape(parameter)}=(\d+)', link.get('href', ''))
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def listing_urls(session, path, parameter):
    url = urljoin(BASE_URL, path)
    first = get_page(session, url)
    soup = BeautifulSoup(first.content, 'html.parser')
    pages = last_page(soup, parameter)
    urls = set()

    for page_number in range(1, pages + 1):
        if page_number > 1:
            response = get_page(session, url, {parameter: page_number})
            soup = BeautifulSoup(response.content, 'html.parser')
        for link in soup.select('a[href*="/de/veranstaltungsdetails/"]'):
            urls.add(urljoin(BASE_URL, link.get('href')))
    return urls


def event_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload.get('@graph', []) if isinstance(payload, dict) else []
        for node in nodes:
            if isinstance(node, dict) and node.get('@type') == 'Event':
                return node
    return {}


def parse_location(soup):
    location = soup.select_one('.eventfull_location')
    if not location:
        return None, None, None
    city_node = location.select_one('span')
    city = clean_text(city_node.get_text(' ', strip=True) if city_node else '')
    city = re.sub(r'\s*\(CH\)\s*$', '', city).strip()
    full_location = clean_text(location.get_text(' ', strip=True))
    venue = full_location[len(city):].lstrip(' ,') if city else ''
    venue = clean_text(venue).strip(' ,')
    if not city or not venue:
        return None, None, None
    return city, venue, FOREIGN_COUNTRIES.get(city, 'DE')


def parse_record(url, content):
    soup = BeautifulSoup(content, 'html.parser')
    event = soup.select_one('.mod_eventreader .event')
    if not event:
        return None
    structured = event_json_ld(soup)
    title = clean_text(structured.get('name')) or clean_text(event.select_one('h1'))
    start = structured.get('startDate') or ''
    match = re.match(r'(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}))?', start)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None
    city, venue, country_code = parse_location(event)
    if not title or not city or not venue or not country_code:
        return None
    time_from = f'{match.group(2)}:{match.group(3)}' if match.group(2) else None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(structured.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return parse_record(url, get_page(session, url).content)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = set()
    for path, parameter in LISTINGS:
        urls.update(listing_urls(session, path, parameter))

    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_record, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class BachakademieDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachakademie_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BachakademieDeCrawler().run()


if __name__ == '__main__':
    main()
