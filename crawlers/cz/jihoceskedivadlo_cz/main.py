import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


BASE_URL = 'https://www.jihoceskedivadlo.cz'
SOURCE = 'Jihočeské divadlo'
SOURCE_URL = f'{BASE_URL}/'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    )
}

# The calendar uses short venue names for the theatre's permanent stages.
HOME_VENUES = {
    'Budova JD', 'Malé divadlo', 'Na Půdě', 'KD Slávie Black Box',
    'KD Slavie Black Box', 'KD Slávie Velký sál', 'KD Slavie Velký sál',
    'Pavilon Z - Výstaviště', 'Český rozhlas', 'Malé divadlo zkušebna',
    'Jihočeské muzeum', 'Katedra sv. Mikuláše',
    'Klášterní kostel Obětování Panny Marie', 'Studio A3D',
}
CITY_MARKERS = {
    'České Budějovice': ('České Budějovice',),
    'Český Krumlov': ('Český Krumlov',),
    'Třeboň': ('Třeboň',),
    'Týn nad Vltavou': ('Týn nad Vltavou',),
    'Holašovice': ('Holašovice',),
    'Kestřany': ('Kestřany',),
    'Brno': ('Brno',),
    'Hluboká nad Vltavou': ('Hluboká nad Vltavou',),
}


def clean_text(value):
    if not value:
        return ''
    value = value.replace('\xa0', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r'\n\s*\n+', '\n\n', value)
    return value.strip()


def canonical_url(url):
    parts = urlsplit(urljoin(BASE_URL, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def split_location(raw_location):
    location = clean_text(raw_location).split(' / ', 1)[0].strip()
    if not location:
        return None, None

    city = None
    for candidate, markers in CITY_MARKERS.items():
        if any(re.search(rf'\b{re.escape(marker)}\b', location, re.I) for marker in markers):
            city = candidate
            break
    if city is None and location in HOME_VENUES:
        city = 'České Budějovice'

    # These are named local sites whose displayed names omit the city.
    if city is None and location in {'Bagr, Park Stromovka', 'Divadlo venku'}:
        city = 'České Budějovice'
    if city is None and location == 'Otáčivé hlediště':
        city = 'Český Krumlov'

    venue = location
    if city and location != city:
        venue = re.sub(rf'\s*,?\s*{re.escape(city)}\s*$', '', location, flags=re.I).strip(' ,-') or location
    return city, venue


def get_soup(session, url):
    response = session.get(url, timeout=40)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def extract_detail(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, url)
    content = soup.select_one('main .page-editor-content') or soup.select_one('main .half-content')
    if not content:
        return None
    return clean_text(content.get_text('\n', strip=True)) or None


def calendar_dates(table):
    dates = []
    for cell in table.select('thead tr > td'):
        year = cell.get('data-year')
        spans = cell.select('p span')
        if not year or len(spans) < 2:
            dates.append(None)
            continue
        match = re.search(r'(\d{1,2})\.(\d{1,2})\.', spans[1].get_text(' ', strip=True))
        if not match:
            dates.append(None)
            continue
        dates.append(datetime(int(year), int(match.group(2)), int(match.group(1))).date().isoformat())
    return dates


def extract_calendar_records(soup):
    table = soup.select_one('#program-kalendar .min_calendar table')
    if not table:
        raise ValueError('Program calendar was not found')

    dates = calendar_dates(table)
    records = []
    for row in table.select('tbody tr'):
        for index, cell in enumerate(row.find_all('td', recursive=False)):
            date = dates[index] if index < len(dates) else None
            if not date:
                continue
            for modal in cell.select('.min_calendar_modal'):
                title_el = modal.select_one('.min_calendar_modal-name strong')
                time_el = modal.select_one('.time')
                location_el = modal.select_one('.location')
                detail_link = modal.select_one('.box_href a[href*="/porad/"]')
                if not all((title_el, time_el, location_el, detail_link)):
                    continue
                time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', time_el.get_text(' ', strip=True))
                city, venue = split_location(location_el.get_text(' ', strip=True))
                if not city or not venue:
                    log_message(
                        'Skipping event with unresolved location',
                        event='crawler_item_skipped', level='warning',
                        url=canonical_url(detail_link.get('href')), location=clean_text(location_el.get_text(' ', strip=True)),
                    )
                    continue
                summary = modal.select_one('.min_calendar_modal-description')
                records.append({
                    'title': clean_text(title_el.get_text(' ', strip=True)),
                    'date': date,
                    'url': canonical_url(detail_link.get('href')),
                    'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
                    'time_to': None,
                    'venue': venue,
                    'city': city,
                    'description': clean_text(summary.get_text('\n', strip=True)) if summary else None,
                })
    return records


class JihoceskeDivadloCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jihoceskedivadlo_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = extract_calendar_records(get_soup(session, SOURCE_URL))

        detail_urls = {record['url'] for record in records}
        details = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(extract_detail, url): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    details[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape production detail', event='crawler_item_failed',
                        level='warning', url=url, error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = details.get(record['url']) or record['description'] or None
        return records


def main():
    return JihoceskeDivadloCrawler().run()


if __name__ == '__main__':
    main()
