import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestrenationaldebretagne.bzh/'
SOURCE = 'Orchestre national de Bretagne'
ARCHIVE_URL = f'{SOURCE_URL}spectacle/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

# The ONB tours, but the site does not expose countries in its occurrence data.
# These are the foreign cities found in its published catalogue; other cities
# on this French orchestra's calendar are treated as French.
FOREIGN_CITIES = {
    'londres': 'GB',
    'london': 'GB',
    'cardiff': 'GB',
    'dublin': 'IE',
    'budapest': 'HU',
    'hambourg': 'DE',
    'hamburg': 'DE',
    'vienne': 'AT',
    'vienna': 'AT',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def new_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    return session


def catalogue_urls(session):
    """Return every detail URL retained by the paginated spectacle archive."""
    urls = set()
    page = 1
    while True:
        url = ARCHIVE_URL if page == 1 else f'{ARCHIVE_URL}page/{page}/'
        response = session.get(url, timeout=45)
        if response.status_code == 404:
            break
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = {
            link['href'].split('#', 1)[0]
            for link in soup.select('.archive__alaune a[href*="/spectacle/"]')
            if link.get('href')
        }
        new_urls = page_urls - urls
        if not new_urls:
            break
        urls.update(new_urls)
        page += 1
    return sorted(urls)


def publication_year(soup):
    meta = soup.select_one('meta[property="article:published_time"]')
    match = re.match(r'(20\d{2})', meta.get('content', '') if meta else '')
    return int(match.group(1)) if match else None


def occurrence_date(value, season_start):
    match = re.fullmatch(r'(\d{1,2})\.(\d{1,2})', clean_text(value))
    if not match or season_start is None:
        return None
    day, month = map(int, match.groups())
    # Season pages are normally published in spring. Autumn performances are
    # in the publication year and January-June performances in the next year.
    year = season_start if month >= 7 else season_start + 1
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def country_for(city):
    folded = clean_text(city).casefold()
    return FOREIGN_CITIES.get(folded, 'FR')


def description_from(soup):
    parts = []
    for selector in ('.spectacle_distribution', '.spectacle_programme', '.spectacle_resume'):
        node = soup.select_one(selector)
        text = clean_text(node)
        if text:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title = clean_text(soup.select_one('main h1'))
    if not title:
        return []

    year = publication_year(soup)
    description = description_from(soup)
    records = []
    for item in soup.select('main .dates__items .date__item'):
        event_date = occurrence_date(item.select_one('.nombres'), year)
        city = clean_text(item.select_one('.lieu'))
        venue = clean_text(item.select_one('.salle'))
        if not event_date or not city or not venue or city.casefold() == venue.casefold():
            continue
        time_match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)\b', clean_text(item.select_one('.heure')))
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_for(city),
            'description': description,
        })
    return records


class OrchestreNationalDeBretagneCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestrenationaldebretagne_bzh',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = new_session()
        urls = catalogue_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(parse_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch ONB concert detail',
                        event='crawler_detail_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        records.sort(key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['venue'], row['city']
        ))
        return records


def main():
    return OrchestreNationalDeBretagneCrawler().run()


if __name__ == '__main__':
    main()
