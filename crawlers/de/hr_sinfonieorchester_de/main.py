import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hr-sinfonieorchester.de/index.html'
CALENDAR_URL = urljoin(SOURCE_URL, 'konzerte/veranstaltungen-110.html')
SOURCE = 'hr-Sinfonieorchester'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The calendar includes tours. These locations have appeared in the orchestra's
# calendar without an addressCountry value. Unknown country-less locations are
# skipped instead of being silently assigned to Germany.
FOREIGN_CITIES = {
    'amsterdam': 'NL', 'antwerpen': 'BE', 'brüssel': 'BE', 'bruxelles': 'BE',
    'budapest': 'HU', 'london': 'GB', 'luxembourg': 'LU', 'luxemburg': 'LU',
    'luzern': 'CH', 'paris': 'FR', 'prag': 'CZ', 'praha': 'CZ',
    'salzburg': 'AT', 'tokio': 'JP', 'tokyo': 'JP', 'wien': 'AT',
    'vienna': 'AT', 'zürich': 'CH', 'zurich': 'CH',
}

COUNTRY_NAMES = {
    'DE': 'DE', 'Deutschland': 'DE', 'Germany': 'DE',
    'AT': 'AT', 'Österreich': 'AT', 'Austria': 'AT',
    'BE': 'BE', 'Belgien': 'BE', 'Belgium': 'BE',
    'CH': 'CH', 'Schweiz': 'CH', 'Switzerland': 'CH',
    'CZ': 'CZ', 'Tschechien': 'CZ', 'Czechia': 'CZ',
    'FR': 'FR', 'Frankreich': 'FR', 'France': 'FR',
    'GB': 'GB', 'United Kingdom': 'GB',
    'HU': 'HU', 'Ungarn': 'HU', 'Hungary': 'HU',
    'JP': 'JP', 'Japan': 'JP', 'LU': 'LU', 'Luxembourg': 'LU',
    'NL': 'NL', 'Niederlande': 'NL', 'Netherlands': 'NL',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_json(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def calendar_pages(html):
    soup = BeautifulSoup(html, 'lxml')
    return {
        urljoin(CALENDAR_URL, link['href'])
        for link in soup.select('a[href*="veranstaltungen-110~_month-"]')
    } | {CALENDAR_URL}


def detail_urls(html, page_url):
    soup = BeautifulSoup(html, 'lxml')
    return {
        urljoin(page_url, link['href'])
        for link in soup.select('.c-eventCalendar__item a.c-teaser__headlineLink[href]')
    }


def country_code(city, address):
    raw_country = clean_text(address.get('addressCountry'))
    if raw_country in COUNTRY_NAMES:
        return COUNTRY_NAMES[raw_country]
    city_key = clean_text(city).casefold()
    if city_key in FOREIGN_CITIES:
        return FOREIGN_CITIES[city_key]
    postal_code = clean_text(address.get('postalCode'))
    if re.fullmatch(r'\d{5}', postal_code):
        return 'DE'
    # Frankfurt is occasionally represented without a postal address.
    if city_key in {'frankfurt', 'frankfurt am main'}:
        return 'DE'
    return None


def occurrence_nodes(soup):
    nodes = soup.select('.c-eventList__item .c-eventInstant')
    return nodes or soup.select('.c-eventInstant')


def parse_occurrence(node, fallback_location):
    text = clean_text(node.select_one('.c-eventInstant__date') or node)
    match = re.search(r'\b(\d{2}\.\d{2}\.\d{2,4})(?:\s+(\d{1,2}:\d{2}))?', text)
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%y' if len(match.group(1)) == 8 else '%d.%m.%Y')
    except ValueError:
        return None

    location = fallback_location or {}
    address = location.get('address') if isinstance(location.get('address'), dict) else {}
    city_node = node.select_one('.c-eventInstant__venue strong, .c-eventInstant__city')
    city = clean_text(city_node) or clean_text(address.get('addressLocality'))
    venue_node = node.select_one('.c-eventInstant__address > .ellipsis')
    venue = clean_text(venue_node) or clean_text(location.get('name'))

    # Current markup has no semantic city/venue classes, but prints them as
    # direct columns after the date. Prefer Schema.org values; only use these
    # short column values when structured data is absent.
    if not city or not venue:
        columns = [clean_text(x) for x in node.select(':scope > .c-eventInstant__column')]
        columns = [x for x in columns if x and not re.search(r'\d{2}\.\d{2}\.\d{2,4}', x)]
        if not city and columns:
            city = columns[0]
        if not venue and len(columns) > 1:
            venue = columns[1]

    code = country_code(city, address)
    if not all((event_date, city, venue, code)):
        return None
    return event_date.date().isoformat(), match.group(2), city, venue, code


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'lxml')
    data = event_json(soup)
    title = clean_text(data.get('name')) or clean_text(soup.select_one('main h1'))
    location = data.get('location') if isinstance(data.get('location'), dict) else {}

    description_parts = [clean_text(data.get('description'))]
    description_parts.extend(clean_text(node) for node in soup.select('.c-event-info__wrap:not(.-image)'))
    works = [clean_text(item.get('name')) for item in data.get('workPerformed', []) if isinstance(item, dict)]
    if any(works):
        description_parts.append('Programm:\n' + '\n'.join(work for work in works if work))
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None

    occurrences = []
    for node in occurrence_nodes(soup):
        parsed = parse_occurrence(node, location)
        if parsed:
            occurrences.append(parsed)

    # Some older detail pages only expose their single occurrence as JSON-LD.
    if not occurrences and data.get('startDate'):
        try:
            start = datetime.fromisoformat(str(data['startDate']).replace('Z', '+00:00'))
        except ValueError:
            start = None
        address = location.get('address') if isinstance(location.get('address'), dict) else {}
        city = clean_text(address.get('addressLocality'))
        venue = clean_text(location.get('name'))
        code = country_code(city, address)
        if start and all((city, venue, code)):
            occurrences.append((start.date().isoformat(), start.strftime('%H:%M'), city, venue, code))

    if not title:
        return []
    return [{
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for date, time_from, city, venue, code in dict.fromkeys(occurrences)]


class HrSinfonieorchesterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hr_sinfonieorchester_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()

        urls = set()
        pages = calendar_pages(response.text)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(session.get, page, timeout=45): page for page in pages}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    page_response = future.result()
                    page_response.raise_for_status()
                    urls.update(detail_urls(page_response.text, page))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch hr-Sinfonieorchester calendar page',
                        event='crawler_fetch_failed', level='warning', url=page,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_response = future.result()
                    detail_response.raise_for_status()
                    records.extend(parse_detail(detail_response.text, url))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch hr-Sinfonieorchester concert detail',
                        event='crawler_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records.sort(key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']))
        return records


def main():
    HrSinfonieorchesterDeCrawler().run()


if __name__ == '__main__':
    main()
