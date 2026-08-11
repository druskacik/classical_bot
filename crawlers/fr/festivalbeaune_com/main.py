import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://festivalbeaune.com/'
SOURCE = 'Festival de Beaune'
PROGRAM_URL = urljoin(SOURCE_URL, 'programmation/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'editions-precedentes/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.6',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def folded(value):
    return clean_text(value).lower().translate(str.maketrans('éèêëàâäîïôöùûüç', 'eeeeaaaiioouuuc'))


def parse_date(value, year):
    match = re.search(r'\b(\d{1,2})\s+([a-zéèêëàâäîïôöùûüç]+)\b', value.lower())
    if not match:
        return None
    month = MONTHS.get(folded(match.group(2)))
    if not month:
        return None
    try:
        return date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?\b', value.lower())
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def programme_year(soup, url):
    match = re.search(r'programmation-(20\d{2})', url)
    if match:
        return int(match.group(1))
    text = clean_text(soup.select_one('h1.page_title') or soup.select_one('title'))
    match = re.search(r'\b(20\d{2})\b', text)
    if match:
        return int(match.group(1))
    # The canonical programme page is the currently advertised edition.
    return date.today().year


def parse_card(card, year):
    title = clean_text(card.select_one('.ev_titre')) or clean_text(card.get('title'))
    date_text = clean_text(card.select_one('.ev_datebot span'))
    event_date = parse_date(date_text, year)
    venue = clean_text(card.select_one('.ev_lieu'))
    url = urljoin(SOURCE_URL, card.get('href', ''))
    if not title or not event_date or not venue or not urlparse(url).netloc:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(card.select_one('.ev_datebot i'))),
        'venue': venue,
        'city': 'Beaune',
        'country_code': 'FR',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def extract_description(soup):
    parts = []
    subtitle = clean_text(soup.select_one('#pev_surtitre'))
    if subtitle:
        parts.append(subtitle)
    for container in soup.select('#pev_titre .contenu, #pev_ctn .contenu, #pev_ctn2 .contenu'):
        text = clean_text(container)
        if text and text not in parts:
            parts.append(text)
    if not parts:
        post = soup.select_one('[id^="post-"]')
        if post:
            text = clean_text(post)
            if text:
                parts.append(text)
    return '\n\n'.join(parts) or None


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    return session


class FestivalBeauneComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalbeaune_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        programme_urls = {PROGRAM_URL}
        try:
            archive_response = session.get(ARCHIVE_URL, timeout=45)
            archive_response.raise_for_status()
            archive_soup = BeautifulSoup(archive_response.text, 'html.parser')
            for link in archive_soup.select('a[href*="programmation-"]'):
                programme_urls.add(urljoin(SOURCE_URL, link.get('href', '')))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Festival de Beaune archive index',
                event='crawler_archive_index_failed',
                level='warning',
                url=ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        records = []
        for programme_url in sorted(programme_urls):
            try:
                response = session.get(programme_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Festival de Beaune programme',
                    event='crawler_programme_failed',
                    level='error',
                    url=programme_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            soup = BeautifulSoup(response.text, 'html.parser')
            year = programme_year(soup, programme_url)
            for card in soup.select('a.evenement[href]'):
                record = parse_card(card, year)
                if record:
                    records.append(record)

        descriptions = {}
        for url in sorted({record['url'] for record in records}):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                descriptions[url] = extract_description(BeautifulSoup(response.text, 'html.parser'))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Festival de Beaune event details',
                    event='crawler_event_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        for record in records:
            record['description'] = descriptions.get(record['url'])

        return sorted(records, key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['venue'], row['url']
        ))


def main():
    FestivalBeauneComCrawler().run()


if __name__ == '__main__':
    main()
