import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operaoviedo.com/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'historico')
SOURCE = 'Ópera de Oviedo'
CITY = 'Oviedo'
VENUE = 'Teatro Campoamor'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11,
    'diciembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def is_event_url(url):
    parsed = urlparse(url)
    if parsed.netloc not in ('operaoviedo.com', 'www.operaoviedo.com'):
        return False
    return bool(re.search(r'/(?:temporada-[^/]+|historico/historico-ficha)/[^/]+/?$', parsed.path))


def season_years(node):
    match = re.search(r'Temporada\s+(20\d{2})\s*/\s*(20\d{2})', clean_text(node))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def discover_catalog(session):
    """Return event URLs with the season years published by the archive."""
    catalog = {}
    home = get_soup(session, SOURCE_URL)
    for link in home.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        if is_event_url(url):
            # The home page is the announced 2026/27 season. Detail pages omit
            # the year, so derive it from their temporada-26-27 URL here.
            match = re.search(r'/temporada-(\d{2})-(\d{2})/', urlparse(url).path)
            if match:
                catalog[url] = (2000 + int(match.group(1)), 2000 + int(match.group(2)))

    archive = get_soup(session, ARCHIVE_URL)
    for timeline in archive.select('.init-timeline'):
        years = season_years(timeline)
        if not years:
            continue
        for link in timeline.select('a[href]'):
            url = urljoin(SOURCE_URL, link.get('href'))
            if is_event_url(url):
                catalog[url] = years
    return catalog


def event_year(month, season):
    start_year, end_year = season
    return start_year if month >= 7 else end_year


def parse_date_text(value, season):
    text = clean_text(value).lower()
    month_match = re.search(r'de\s+([a-záéíóúüñ]+)', text)
    if not month_match or month_match.group(1) not in MONTHS:
        return None
    month = MONTHS[month_match.group(1)]
    day_match = re.search(r'\b(\d{1,2})\b', text)
    if not day_match:
        return None
    try:
        return date(event_year(month, season), month, int(day_match.group(1))).isoformat()
    except ValueError:
        return None


def description_from_detail(soup):
    node = soup.select_one('.text-block .text')
    description = clean_text(node)
    if description:
        return description
    meta = soup.select_one('meta[name="description"][content]')
    return clean_text(meta.get('content')) if meta else None


def parse_detail(soup, url, season):
    title = clean_text(soup.select_one('title'))
    if not title:
        return []
    description = description_from_detail(soup)
    records = []
    schedules = soup.select('.opera-schedules .schedule')
    if schedules:
        occurrences = []
        for schedule in schedules:
            text = clean_text(schedule)
            event_date = parse_date_text(text, season)
            time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\s*h?\b', text)
            occurrences.append((event_date, f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None))
    else:
        dates = soup.select_one('.dates')
        text = clean_text(dates).lower()
        month_match = re.search(r'de\s+([a-záéíóúüñ]+)', text)
        occurrences = []
        if month_match and month_match.group(1) in MONTHS:
            month = MONTHS[month_match.group(1)]
            for raw_day in re.findall(r'\b\d{1,2}\b', text[:month_match.start()]):
                try:
                    event_date = date(event_year(month, season), month, int(raw_day)).isoformat()
                except ValueError:
                    continue
                occurrences.append((event_date, None))

    for event_date, time_from in occurrences:
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': VENUE,
            'city': CITY,
            'country_code': 'ES',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    catalog = discover_catalog(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_soup, session, url): (url, season)
            for url, season in catalog.items()
        }
        for future in as_completed(futures):
            url, season = futures[future]
            try:
                records.extend(parse_detail(future.result(), url, season))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape opera detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    unique = {
        (record['url'], record['date'], record['time_from']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class OperaOviedoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaoviedo_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaOviedoComCrawler().run()


if __name__ == '__main__':
    main()
