import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://osm.es/'
SOURCE = 'Orquesta Sinfónica de Madrid'
CALENDAR_URL = urljoin(SOURCE_URL, 'temporada/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

# Calendar venue labels are consistent, but most omit the municipality.  Keep
# the touring locations explicit instead of applying the orchestra's home city.
VENUE_CITIES = {
    'auditorio nacional de música': 'Madrid',
    'auditorio del instituto ramiro de maeztu': 'Madrid',
    'real teatro del retiro': 'Madrid',
    'teatro la latina': 'Madrid',
    'teatro real': 'Madrid',
    'teatro real. sala principal': 'Madrid',
    'auditorio montserrat caballé - arganda del rey': 'Arganda del Rey',
    'auditorio pilar bardem - rivas-vaciamadrid': 'Rivas-Vaciamadrid',
    'palacio de carlos v - granada': 'Granada',
}

TIME_RE = re.compile(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized_venue(value):
    return clean_text(value).strip(' .')


def city_for_venue(venue):
    key = venue.casefold().strip(' .')
    if key in VENUE_CITIES:
        return VENUE_CITIES[key]
    # These forms cover harmless capitalization/punctuation changes while not
    # treating an unknown touring hall as a Madrid performance.
    if 'arganda del rey' in key:
        return 'Arganda del Rey'
    if 'rivas-vaciamadrid' in key:
        return 'Rivas-Vaciamadrid'
    if 'granada' in key:
        return 'Granada'
    return None


def parse_calendar_page(html, year, month):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for box in soup.select('.calendario__obra'):
        link = box.select_one('a[href*="/actividades/"]')
        day_node = box.find_parent('li')
        day_node = day_node.select_one('.dia') if day_node else None
        title_node = box.select_one('.nombre')
        facts = box.select_one('.fecha-y-lugar')
        venue_node = facts.select_one('span') if facts else None
        if not all((link, day_node, title_node, venue_node)):
            continue

        title = clean_text(title_node)
        venue = normalized_venue(venue_node)
        city = city_for_venue(venue)
        try:
            event_date = date(year, month, int(clean_text(day_node))).isoformat()
        except ValueError:
            continue
        if not title or not venue or not city:
            continue

        time_match = TIME_RE.search(clean_text(facts))
        records.append({
            'title': title,
            'date': event_date,
            'url': urljoin(SOURCE_URL, link.get('href')),
            'time_from': (
                f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
                if time_match else None
            ),
            'venue': venue,
            'city': city,
        })
    return records


def detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    # This is the long programme/body column. It excludes the title/ticket
    # header as well as the global navigation and related-events carousel.
    body = soup.select_one('div.col-12.col-lg-8.color-white.lh-sm')
    text = clean_text(body)
    return text or None


def fetch_calendar(session, year, month):
    response = session.get(
        CALENDAR_URL,
        params={'month': month, 'ano': year},
        timeout=45,
    )
    response.raise_for_status()
    return parse_calendar_page(response.text, year, month)


def fetch_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return detail_description(response.text)


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    current_year = date.today().year
    # The calendar accepts arbitrary month/year queries. Eleven complete past
    # years covers every retained event on the current site, plus announced
    # seasons up to two years ahead.
    months = [
        (year, month)
        for year in range(current_year - 10, current_year + 3)
        for month in range(1, 13)
    ]
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_calendar, session, year, month): (year, month)
            for year, month in months
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch OSM calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{CALENDAR_URL}?month={month}&ano={year}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    descriptions = {}
    urls = {record['url'] for record in records}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_description, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch OSM event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                descriptions[url] = None

    for record in records:
        record['description'] = descriptions.get(record['url'])
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class OsmEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osm_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    OsmEsCrawler().run()


if __name__ == '__main__':
    main()
