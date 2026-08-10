import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jenaer-philharmonie.de/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Jenaer Philharmonie'

COUNTRY_CODES = {
    'china': 'CN',
    'dänemark': 'DK',
    'deutschland': 'DE',
    'germany': 'DE',
    'italien': 'IT',
    'liechtenstein': 'LI',
    'niederlande': 'NL',
    'schweden': 'SE',
    'schweiz': 'CH',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def concert_urls(session):
    soup = get_soup(session, SITEMAP_URL, 'xml')
    return sorted({
        location.get_text(strip=True)
        for location in soup.select('loc')
        if '/konzert/' in location.get_text()
    })


def event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload.get('@graph', [payload]) if isinstance(payload, dict) else []
        for node in nodes:
            if isinstance(node, dict) and node.get('@type') == 'Event':
                return node
    return {}


def location_data(soup, event):
    reader = soup.select_one('.mod_eventreader')
    route_link = reader.select_one('a[href*="/anfahrt.html?"]') if reader else None
    query = parse_qs(urlparse(route_link.get('href', '')).query) if route_link else {}

    def parameter(name):
        return clean_text((query.get(name) or [''])[0])

    venue_name = parameter('name')
    room = parameter('room')
    venue = ' / '.join(part for part in (venue_name, room) if part)
    if not venue:
        location = event.get('location') or {}
        venue = clean_text(location.get('name')) if isinstance(location, dict) else ''

    city = parameter('city')
    city = re.sub(r'^\s*\d{4,5}\s*(?:[A-Z]{1,2}\s+)?', '', city).strip()
    if city.startswith('Toblach'):
        city = 'Toblach'

    country = parameter('country').casefold()
    country_code = COUNTRY_CODES.get(country, 'DE' if not country else '')

    # A few old records omit the route's city even though their venue or title
    # identifies it unambiguously.
    if not city and venue in {
        'Kirche Isserstedt', 'Verschiedene Orte im Stadtgebiet',
        'Volkshaus', 'JenaTV',
    }:
        city = 'Jena'
    elif not city and venue == 'Changsha Poly Concert Hall':
        city = 'Changsha'
    elif not city and venue == 'Qintai Concert Hall':
        city = 'Wuhan'
    elif not city and venue == 'Nanjing Poly Theatre':
        city = 'Nanjing'
    return venue, city, country_code


def make_record(url, soup):
    event = event_data(soup)
    title = clean_text(event.get('name'))
    start = event.get('startDate')
    venue, city, country_code = location_data(soup, event)

    try:
        start_at = datetime.fromisoformat(start)
        event_date = start_at.date().isoformat()
        time_from = start_at.strftime('%H:%M')
    except (TypeError, ValueError):
        return None

    description_node = soup.select_one('.mod_eventreader > .m9 > .text')
    description = clean_text(description_node) or clean_text(event.get('description')) or None
    if not all((title, event_date, url, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = make_record(url, future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Jenaer Philharmonie concert detail',
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
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class JenaerPhilharmonieDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jenaer_philharmonie_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    JenaerPhilharmonieDeCrawler().run()


if __name__ == '__main__':
    main()
