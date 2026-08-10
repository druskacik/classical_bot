import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.guerzenich-orchester.de/de/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programm')
SOURCE = 'Gürzenich-Orchester Köln'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The programme currently uses these Cologne venues. Keeping this an explicit
# allow-list avoids assigning the orchestra's home city to a future tour stop.
VENUE_CITIES = {
    'belgisches haus cäcilienstraße 46 50667 köln': ('Köln', 'DE'),
    'belgisches haus, cäcilienstraße 46, 50667 köln': ('Köln', 'DE'),
    'bürgerzentrum engelshof e.v.': ('Köln', 'DE'),
    'kammermusiksaal am kartäuserwall 40, 50676 köln': ('Köln', 'DE'),
    'kölner philharmonie': ('Köln', 'DE'),
    'rheinische musikschule probenaula': ('Köln', 'DE'),
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


def parse_locations(soup):
    locations = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get('@type') != 'Event':
            continue
        entries = data.get('location') or []
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            venue = clean_text(entry.get('name'))
            addresses = entry.get('address') or []
            if isinstance(addresses, dict):
                addresses = [addresses]
            address = next((item for item in addresses if isinstance(item, dict)), {})
            city = clean_text(address.get('addressLocality'))
            country = clean_text(address.get('addressCountry')).upper()
            if venue and city:
                locations[venue.lower()] = (city, country or 'DE')
    return locations


def detail_description(soup):
    parts = []
    programme = clean_text(soup.select_one('.m-program'))
    if programme:
        parts.append('Programm\n' + programme)
    for block in soup.select('.m-text'):
        text = clean_text(block)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail(soup):
    return {
        'description': detail_description(soup),
        'locations': parse_locations(soup),
    }


def parse_card(card, details):
    title = clean_text(card.select_one('.m-date__title'))
    href = (card.select_one('.m-date__title') or {}).get('href', '')
    url = urljoin(SOURCE_URL, href)
    date_text = clean_text(card.select_one('.m-date__day-js'))
    venue = clean_text(card.select_one('.m-date__location'))
    time_text = clean_text(card.select_one('.m-date__time'))

    try:
        date = datetime.strptime(date_text, '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None
    time_match = re.search(r'(?<!\d)([01]\d|2[0-3]):[0-5]\d(?!\d)', time_text)

    location = details.get('locations', {}).get(venue.lower())
    if not location:
        location = VENUE_CITIES.get(venue.lower())
    if not title or not href or not venue or not location:
        return None
    city, country_code = location
    if not city or not country_code:
        return None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': details.get('description'),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    listing = get_soup(session, PROGRAM_URL)
    cards = listing.select('.m-date')
    urls = {
        urljoin(SOURCE_URL, link.get('href'))
        for link in listing.select('a.m-date__title[href]')
    }
    details = {}

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = parse_detail(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Gürzenich-Orchester event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for card in cards:
        link = card.select_one('a.m-date__title[href]')
        url = urljoin(SOURCE_URL, link.get('href')) if link else ''
        record = parse_card(card, details.get(url, {}))
        if record:
            records.append(record)
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class GuerzenichOrchesterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='guerzenich_orchester_de',
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
    GuerzenichOrchesterDeCrawler().run()


if __name__ == '__main__':
    main()
