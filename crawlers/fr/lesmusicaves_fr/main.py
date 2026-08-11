import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lesmusicaves.fr/'
SOURCE = 'Festival Les Musicaves'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programmation/')
DEFAULT_VENUE = 'Domaine Thénard'
DEFAULT_CITY = 'Givry'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
    'decembre': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text, year):
    match = re.search(
        r'\b(\d{1,2})\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|'
        r'août|aout|septembre|octobre|novembre|décembre|decembre)\b',
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return date(year, MONTHS[match.group(2).lower()], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?\b', text, re.IGNORECASE)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def extract_venue(soup, title):
    for paragraph in soup.select('.elementor-widget-text-editor p'):
        text = clean_text(paragraph)
        match = re.match(r'^\s*📍\s*(.+?)\s*$', text)
        if match:
            venue = match.group(1).strip(' .–-')
            if venue.lower().startswith('point gps'):
                return None
            if venue:
                return re.sub(r'^Domaine Thenard$', DEFAULT_VENUE, venue, flags=re.IGNORECASE)
    off_venue = re.search(r'\bOFF\s+(?:à|au)\s+(.+)$', title, re.IGNORECASE)
    if off_venue:
        return off_venue.group(1).strip()
    return DEFAULT_VENUE


def extract_description(soup):
    sections = []
    for widget in soup.select('.elementor-widget-text-editor'):
        text = clean_text(widget)
        if not text or text.startswith(('📍', '🕠')):
            continue
        if any(marker in text for marker in ('Inscrivez vous à notre newsletter', 'Association Muzicaves')):
            continue
        sections.append(text)
    description = '\n\n'.join(dict.fromkeys(sections)).strip()
    return description or None


def parse_detail(soup, url, year):
    title = clean_text(soup.select_one('h1'))
    page_text = clean_text(soup)
    event_date = parse_date(page_text, year)
    venue = extract_venue(soup, title)
    if not title or not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(page_text),
        'venue': venue,
        'city': DEFAULT_CITY,
        'country_code': 'FR',
        'description': extract_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LesmusicavesFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lesmusicaves_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
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
            response = session.get(PROGRAMME_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Les Musicaves programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAMME_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        programme_soup = BeautifulSoup(response.text, 'html.parser')
        heading = clean_text(programme_soup.select_one('h1'))
        year_match = re.search(r'\b(20\d{2})\b', heading)
        if not year_match:
            raise ValueError('Could not determine the programme year')
        year = int(year_match.group(1))

        urls = []
        for link in programme_soup.select('a[href*="/artistes/"]'):
            url = urljoin(PROGRAMME_URL, link.get('href', ''))
            parsed = urlparse(url)
            if parsed.netloc == urlparse(SOURCE_URL).netloc and '/artistes/' in parsed.path:
                urls.append(url.split('#', 1)[0])

        records = []
        for url in dict.fromkeys(urls):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Les Musicaves event detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_detail(BeautifulSoup(detail_response.text, 'html.parser'), url, year)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    LesmusicavesFrCrawler().run()


if __name__ == '__main__':
    main()
