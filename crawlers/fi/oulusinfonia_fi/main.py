import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://oulusinfonia.fi/'
CONCERTS_URL = urljoin(SOURCE_URL, 'kaikki-konsertit/')
SOURCE = 'Oulu Sinfonia'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}

# Most performances are in Oulu. These tokens identify the touring venues
# retained in the archive; parentheses in a venue are also checked for a city.
CITY_TOKENS = {
    'jyväskyl': ('Jyväskylä', 'FI'),
    'naantali': ('Naantali', 'FI'),
    'helsinki': ('Helsinki', 'FI'),
    'musiikkitalo': ('Helsinki', 'FI'),
    'finlandia-talo': ('Helsinki', 'FI'),
    'temppeliaukio': ('Helsinki', 'FI'),
    'kulttuuritalo': ('Helsinki', 'FI'),
    'tampere': ('Tampere', 'FI'),
    'turku': ('Turku', 'FI'),
    'kuopio': ('Kuopio', 'FI'),
    'lahti': ('Lahti', 'FI'),
    'rovaniemi': ('Rovaniemi', 'FI'),
    'ukko-luosto': ('Sodankylä', 'FI'),
    'kajaani': ('Kajaani', 'FI'),
    'kokkola': ('Kokkola', 'FI'),
    'tyrnäv': ('Tyrnävä', 'FI'),
    'muhos': ('Muhos', 'FI'),
    'liminka': ('Liminka', 'FI'),
    'raahe': ('Raahe', 'FI'),
    'raahen': ('Raahe', 'FI'),
    'kempele': ('Kempele', 'FI'),
    'yli-iin': ('Oulu', 'FI'),
    'haukiputaa': ('Oulu', 'FI'),
    'kiiminki': ('Oulu', 'FI'),
    'oulunsalo': ('Oulu', 'FI'),
    'harstad': ('Harstad', 'NO'),
    'piteå': ('Piteå', 'SE'),
    'luulaja': ('Luleå', 'SE'),
    'luleå': ('Luleå', 'SE'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def concert_urls(session):
    urls = set()
    for period in ('tulevat', 'menneet'):
        soup = get_soup(session, CONCERTS_URL, {'ajankohta': period})
        urls.update(
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select('a[href*="/konsertit/"]')
            if link.get('href')
        )
    return sorted(urls)


def parse_date(value):
    match = re.search(r'\b(\d{2}\.\d{2}\.\d{4})\b', value or '')
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\b', value or '')
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def resolve_location(venue):
    normalized = venue.casefold()
    for token, location in CITY_TOKENS.items():
        if token in normalized:
            return location
    # A venue explicitly naming another municipality should not receive the
    # orchestra's home-city default. Unknown touring locations are skipped.
    if re.search(r'\b(?:kirkko|sali|talo|areena|teatteri)\s*\(([^)]+)\)', normalized):
        return None, None
    return 'Oulu', 'FI'


def description_text(soup):
    sections = [section for section in soup.select('section.container') if section.select_one('h1') is None]
    if not sections:
        return None
    first_row = sections[0].find('div', class_='row', recursive=False)
    return clean_text(first_row) or None


def performance_rows(soup):
    rows = []
    for table in soup.select('.roll table.table'):
        cells = [clean_text(cell) for cell in table.select('th, td')]
        event_date = parse_date(cells[0] if cells else '')
        time_from = parse_time(cells[1] if len(cells) > 1 else '')
        venue = cells[2] if len(cells) > 2 else ''
        if event_date and venue:
            rows.append((event_date, time_from, venue))
    if rows:
        return rows
    header = soup.select_one('section.container[class*="category-"]')
    date_text = clean_text(header.select_one('.paivamaara-header')) if header else ''
    event_date = parse_date(date_text)
    time_text = clean_text(header.select_one('.kellonaika-header')) if header else ''
    time_from = parse_time(time_text or re.sub(r'\d{2}\.\d{2}\.\d{4}', '', date_text))
    venue = clean_text(header.select_one('.konserttipaikka-header')) if header else ''
    return [(event_date, time_from, venue)] if event_date and venue else []


def parse_concert(soup, url):
    title = clean_text(soup.select_one('h1'))
    description = description_text(soup)
    records = []
    for event_date, time_from, venue in performance_rows(soup):
        city, country_code = resolve_location(venue)
        if not all((title, event_date, url, venue, city, country_code)):
            log_message(
                'Skipping Oulu Sinfonia performance with incomplete required fields',
                event='crawler_item_skipped', level='warning', url=url,
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
        })
    return records


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
                records.extend(parse_concert(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Oulu Sinfonia concert detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue'],
    ))


class OuluSinfoniaFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oulusinfonia_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
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
    OuluSinfoniaFiCrawler().run()


if __name__ == '__main__':
    main()
