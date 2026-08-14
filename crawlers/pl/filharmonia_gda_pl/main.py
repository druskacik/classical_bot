import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filharmonia.gda.pl/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalendarium-koncertow')
SOURCE = 'Polska Filharmonia Bałtycka im. Fryderyka Chopina w Gdańsku'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

CITY_HINTS = {
    'gdańsk': 'Gdańsk',
    'gdynia': 'Gdynia',
    'gniewin': 'Gniewino',
    'jastarni': 'Jastarnia',
    'jastrzębiej górze': 'Jastrzębia Góra',
    'kątach rybackich': 'Kąty Rybackie',
    'kościerzynie': 'Kościerzyna',
    'lubieszewie': 'Lubieszewo',
    'malborku': 'Malbork',
    'niedźwiedzicy': 'Niedźwiedzica',
    'nowym dworze gdańskim': 'Nowy Dwór Gdański',
    'nowym stawie': 'Nowy Staw',
    'pelplinie': 'Pelplin',
    'pucku': 'Puck',
    'rumi': 'Rumia',
    'stegnie': 'Stegna',
    'wejherowskie': 'Wejherowo',
    'żarnowcu': 'Żarnowiec',
}

GDAŃSK_VENUES = (
    'archikatedra oliwska', 'bazylika św. mikołaja', 'filharmonia na ołowiance',
    'foyer główne', 'foyer motława', 'foyer rozeta', 'góra gradowa',
    'park oliwski', 'ratusz staromiejski', 'sala biała', 'sala dębowa',
    'sala kameralna', 'sala koncertowa', 'sala nad motławą', 'sala pfb',
    'salon gdański', 'kościół św. barbary', 'kościół św. brygidy',
    'kościół św. jakuba', 'kościół św. józefa', 'kościół św. katarzyny',
    'kościół św. piotra i pawła', 'kościół św. trójcy',
    'kościół zielonoświątkowy',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def calendar_years(html):
    soup = BeautifulSoup(html, 'html.parser')
    years = {
        int(option['value'])
        for option in soup.select('select[name="year"] option[value]')
        if option['value'].isdigit()
    }
    return sorted(years)


def parse_calendar_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    return {
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('a.eb_event_link[href*="/repertuar/"]')
    }


def parse_date_and_time(text):
    match = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{4})(?:,\s*(\d{1,2}):(\d{2}))?', text)
    if not match:
        return None, None
    try:
        event_date = date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(4) and int(match.group(4)) < 24 and int(match.group(5)) < 60:
        event_time = f'{int(match.group(4)):02d}:{match.group(5)}'
    return event_date, event_time


def event_properties(soup):
    properties = {}
    for row in soup.select('#eb-event-info tr.eb-event-property'):
        label = clean_text(row.select_one('.eb-event-property-label')).lower()
        value = row.select_one('.eb-event-property-value')
        if label and value:
            properties[label] = value
    return properties


def city_from_venue(venue):
    lowered = venue.lower()
    for hint, city in CITY_HINTS.items():
        if hint in lowered:
            return city
    if any(hint in lowered for hint in GDAŃSK_VENUES):
        return 'Gdańsk'
    return None


def parse_detail_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('#eb-event-page h1.eb-page-heading'))
    properties = event_properties(soup)
    date_cell = properties.get('data wydarzenia')
    venue_cell = properties.get('miejsce')
    event_date, event_time = parse_date_and_time(clean_text(date_cell))
    venue = clean_text(venue_cell)
    if not title or not event_date or not venue:
        return None

    description = clean_text(soup.select_one('#eb-event-details .eb-description-details')) or None
    map_link = venue_cell.select_one('a[href*="view-map"]') if venue_cell else None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city_from_venue(venue),
        'country_code': 'PL',
        'description': description,
        '_map_url': urljoin(SOURCE_URL, map_link['href']) if map_link else None,
    }


def parse_map_city(html):
    soup = BeautifulSoup(html, 'html.parser')
    options = soup.select_one('script.joomla-script-options[type="application/json"]')
    if not options:
        return None
    try:
        popup = json.loads(options.string or '{}').get('popupContent', '')
    except json.JSONDecodeError:
        return None
    text = clean_text(popup)
    postal = re.search(r'\b\d{2}-\d{3}\s+([^,\n]+)', text)
    if postal:
        return postal.group(1).strip()
    first_line = next((line for line in text.splitlines()[1:] if line and 'wskazówki' not in line.lower()), '')
    candidate = first_line.split(',')[0].strip()
    return candidate or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_html = get_page(session, CALENDAR_URL)
    years = calendar_years(first_html)
    if not years:
        years = [date.today().year]

    calendar_urls = [f'{CALENDAR_URL}?month={month}&year={year}' for year in years for month in range(1, 13)]
    event_urls = set()
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_page, session, url): url for url in calendar_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                event_urls.update(parse_calendar_page(future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month', event='crawler_page_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error),
                )

    records = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(get_page, session, url): url for url in event_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_detail_page(future.result(), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail', event='crawler_page_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error),
                )

    unresolved = {record['_map_url'] for record in records if not record['city'] and record['_map_url']}
    map_cities = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_page, session, url): url for url in unresolved}
        for future in as_completed(futures):
            url = futures[future]
            try:
                map_cities[url] = parse_map_city(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape venue map', event='crawler_page_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error),
                )

    valid = []
    for record in records:
        record['city'] = record['city'] or map_cities.get(record['_map_url'])
        record.pop('_map_url', None)
        if record['city']:
            valid.append(record)
    return sorted(valid, key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']))


class FilharmoniaGdaPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonia_gda_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    FilharmoniaGdaPlCrawler().run()


if __name__ == '__main__':
    main()
