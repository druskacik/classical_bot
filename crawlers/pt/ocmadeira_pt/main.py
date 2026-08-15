import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ocmadeira.pt/'
EVENTS_URL = urljoin(SOURCE_URL, 'concertos/temporada-atual.html')
SOURCE = 'Orquestra Clássica da Madeira'
HEADERS = {
    # The server rejects generic HTTP-library user agents.
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
    'Upgrade-Insecure-Requests': '1',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def fetch_soup(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch OCM page',
            event='crawler_fetch_failed',
            level='error',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise
    return BeautifulSoup(response.text, 'html.parser')


def parse_datetime(value):
    value = clean_text(value)
    for pattern in ('%d-%m-%Y %I:%M %p', '%d-%m-%Y %H:%M', '%d-%m-%Y'):
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.date().isoformat(), (
                parsed.strftime('%H:%M') if '%M' in pattern else None
            )
        except ValueError:
            continue
    return None, None


def event_properties(soup):
    properties = {}
    for row in soup.select('tr.eb-event-property'):
        cells = row.select('td')
        if len(cells) >= 2:
            properties[clean_text(cells[0]).casefold()] = cells[1]
    return properties


def city_from_location_page(session, location_url):
    if not location_url:
        return None
    soup = fetch_soup(session, location_url)
    options = soup.select_one('script.joomla-script-options')
    if not options:
        return None
    try:
        popup = json.loads(options.string or '{}').get('popupContent', '')
    except json.JSONDecodeError:
        return None
    address = clean_text(BeautifulSoup(popup, 'html.parser').select_one('p'))
    if not address:
        return None
    postal_city = re.search(r'\b\d{4}-\d{3}\s+([^,]+)', address)
    if postal_city:
        city = postal_city.group(1).strip()
        if city.casefold() != 'madeira':
            return city
    # Event Booking location records use "municipality, postcode ...".
    city = address.split(',', 1)[0].strip()
    if not city or any(char.isdigit() for char in city):
        return None
    return city


def parse_detail(session, url):
    soup = fetch_soup(session, url)
    title = clean_text(soup.select_one('h1.eb-page-heading'))
    properties = event_properties(soup)
    event_date, time_from = parse_datetime(properties.get('data'))
    venue_cell = properties.get('local')
    venue = clean_text(venue_cell)
    location_link = venue_cell.select_one('a[href]') if venue_cell else None
    location_url = urljoin(url, location_link['href']) if location_link else None
    city = city_from_location_page(session, location_url)

    description_node = soup.select_one('.eb-description-details')
    description = clean_text(description_node) or None

    if not all((title, event_date, venue, city)):
        log_message(
            'Skipping OCM event with missing required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(event_date),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'PT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OcmadeiraPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ocmadeira_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = build_session()
        soup = fetch_soup(session, EVENTS_URL)
        event_urls = list(dict.fromkeys(
            urljoin(EVENTS_URL, link['href'])
            for link in soup.select('a.eb-event-title[href]')
        ))
        if not event_urls:
            raise ValueError('No events found on the OCM current-season page')

        records = []
        for url in event_urls:
            record = parse_detail(session, url)
            if record:
                records.append(record)
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OcmadeiraPtCrawler().run()


if __name__ == '__main__':
    main()
