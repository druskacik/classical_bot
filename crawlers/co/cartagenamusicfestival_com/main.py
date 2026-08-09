import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cartagenamusicfestival.com/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programacion/')
SOURCE = 'Cartagena Festival de Música'
CITY = 'Cartagena'

HEADERS = {
    # The site permits indexing agents while its interactive pages are behind
    # a Cloudflare browser challenge.
    'User-Agent': 'Googlebot',
    'Accept-Language': 'es-CO,es;q=0.9',
}

MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'septiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(20\d{2})'
        r'\s*-\s*(\d{1,2}):([0-5]\d)\s*([ap])\.?\s*m\.?',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None

    hour = int(match.group(4)) % 12
    if match.group(6).lower() == 'p':
        hour += 12
    return event_date, f'{hour:02d}:{match.group(5)}'


def role_values(soup, role_name):
    for role in soup.select('.mkdf-show-role'):
        heading = clean_text(role.select_one('.mkdf-show-role-title'))
        if heading.casefold() == role_name.casefold():
            return [clean_text(item) for item in role.select('li') if clean_text(item)]
    return []


def parse_detail(soup, url):
    title = clean_text(soup.select_one('h2.mkdf-page-title'))
    description_box = soup.select_one('.mkdf-single-show-description')
    date_heading = description_box.select_one('h6') if description_box else None
    event_date, time_from = parse_datetime(clean_text(date_heading))
    venues = role_values(soup, 'ESCENARIO')
    venue = venues[0] if venues else ''
    if not title or not event_date or not venue:
        return None

    if description_box and date_heading:
        date_heading.extract()
    description = clean_text(description_box) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'CO',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_urls(programme_soup):
    urls = []
    seen = set()
    for link in programme_soup.select('a[href*="/show-item/"][href]'):
        url = urljoin(PROGRAM_URL, link['href'])
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


class CartagenaMusicFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cartagenamusicfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CO',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            programme_soup = fetch_soup(session, PROGRAM_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Cartagena festival programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAM_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = detail_urls(programme_soup)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_soup, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = parse_detail(future.result(), url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Cartagena festival event',
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


def main():
    CartagenaMusicFestivalComCrawler().run()


if __name__ == '__main__':
    main()
