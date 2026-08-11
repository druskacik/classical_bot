import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera.saint-etienne.fr/otse/'
SOURCE = 'Opéra de Saint-Étienne'
ARCHIVE_URL = urljoin(SOURCE_URL, 'saisons-passees/')
MONTHS = {
    'janv': 1, 'fevr': 2, 'mars': 3, 'avr': 4, 'mai': 5, 'juin': 6,
    'juil': 7, 'aout': 8, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}


def clean_text(node):
    if node is None:
        return ''
    value = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    value = value.replace('\xa0', ' ').replace('\u202f', ' ')
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n\s*\n+', '\n', value)).strip()


def canonical_url(value):
    parsed = urlparse(urljoin(SOURCE_URL, value))
    return urlunparse(('https', parsed.netloc, parsed.path, '', '', ''))


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def season_years(url):
    match = re.search(r'/saison-(\d{2})-(\d{2})/', url, re.I)
    if not match:
        return None
    return 2000 + int(match.group(1)), 2000 + int(match.group(2))


def parse_occurrences(value, url):
    years = season_years(url)
    if not years:
        return []
    folded = clean_text(value).casefold()
    folded = (folded.replace('é', 'e').replace('è', 'e').replace('ê', 'e')
              .replace('û', 'u').replace('ô', 'o').replace('à', 'a'))
    pattern = re.compile(
        r'(\d{1,2})\s+(janv|fevr|mars|avr|mai|juin|juil|aout|sept|oct|nov|dec)[a-z.]*'
        r'(?:\s*(?::|[•-])?\s*(\d{1,2})\s*h(?:\s*([0-5]\d))?)?',
        re.I,
    )
    occurrences = []
    for match in pattern.finditer(folded):
        month = MONTHS[match.group(2).lower()]
        year = years[0] if month >= 8 else years[1]
        try:
            event_date = date(year, month, int(match.group(1))).isoformat()
        except ValueError:
            continue
        event_time = None
        if match.group(3) is not None:
            event_time = f'{int(match.group(3)):02d}:{int(match.group(4) or 0):02d}'
        occurrences.append((event_date, event_time))
    return occurrences


def parse_detail(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    spectacle = soup.select_one('#spectacle')
    if not spectacle:
        return []

    heading = spectacle.select_one('#sd h2') or spectacle.select_one('h2')
    title = clean_text(heading)
    venue = clean_text(spectacle.select_one('#s_details .lieu'))
    occurrences = parse_occurrences(spectacle.select_one('#s_details .date'), url)
    if not title or not venue or not occurrences:
        return []

    description_parts = []
    for selector in ('#sd_presentation', '#sd_distribution'):
        text = clean_text(spectacle.select_one(selector))
        if text:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': 'Saint-Étienne',
        'description': description,
    } for event_date, event_time in occurrences]


class OperaSaintEtienneCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_saint_etienne_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = make_session()
        response = session.get(ARCHIVE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        season_ids = [option.get('value') for option in soup.select('#recherche_saison option[value]')]
        season_ids = [value for value in season_ids if value]

        urls = set()
        for season_id in season_ids:
            try:
                page = session.get(ARCHIVE_URL, params={'recherche_saison': season_id}, timeout=30)
                page.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Archive season request failed', level='warning', url=ARCHIVE_URL,
                    season_id=season_id, error_type=type(error).__name__, error_message=str(error),
                )
                continue
            season_soup = BeautifulSoup(page.text, 'html.parser')
            for link in season_soup.select('.spectacle h4 a[href]'):
                urls.add(canonical_url(link['href']))

        records = []
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(parse_detail, make_session(), url): url for url in urls}
            for future in as_completed(futures):
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Spectacle request failed', level='warning', url=futures[future],
                        error_type=type(error).__name__, error_message=str(error),
                    )
                except Exception as error:
                    log_message(
                        'Spectacle parse failed', level='warning', url=futures[future],
                        error_type=type(error).__name__, error_message=str(error),
                    )
        log_message('Scrape completed', level='info', record_count=len(records), detail_url_count=len(urls))
        return records


def main():
    return OperaSaintEtienneCrawler().run()


if __name__ == '__main__':
    main()
