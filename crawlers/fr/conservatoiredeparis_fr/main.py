import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.conservatoiredeparis.fr/fr'
LISTING_URL = f'{SOURCE_URL}/la-saison'
SOURCE = 'Conservatoire national supérieur de musique et de danse de Paris'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'jan': 1, 'fev': 2, 'mar': 3, 'avr': 4, 'mai': 5, 'juin': 6,
    'jul': 7, 'juil': 7, 'aou': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

KNOWN_CITIES = (
    'Breuillet', 'Colmar', 'Concarneau', 'Elliant', 'Fontainebleau', 'Fresnes',
    'Levallois-Perret', 'Malakoff', 'Paris', 'Pontoise', 'Soissons',
    'Tremblay-en-France', 'Trégunc',
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def ascii_lower(value):
    return ''.join(
        character for character in unicodedata.normalize('NFD', value.lower())
        if unicodedata.category(character) != 'Mn'
    )


def parse_date(date_node):
    text = clean_text(date_node)
    match = re.search(r'(?<!\d)(\d{1,2})\s*([A-Za-zÀ-ÿ]{3,})\s*(20\d{2})', text)
    if not match:
        return None
    month = MONTHS.get(ascii_lower(match.group(2))[:4].rstrip('.'))
    if not month:
        month = MONTHS.get(ascii_lower(match.group(2))[:3])
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)', value or '')
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def infer_city(venue):
    folded = ascii_lower(venue)
    for city in KNOWN_CITIES:
        if ascii_lower(city) in folded:
            return city
    if 'conservatoire de paris' in folded or 'cite de la musique' in folded:
        return 'Paris'
    if any(name in folded for name in (
        'palais garnier', 'radio france', 'theatre national de chaillot',
        'theatre des champs-elysees', 'invalides',
    )):
        return 'Paris'
    return None


def listing_urls(session):
    urls = []
    page = 0
    while True:
        response = session.get(LISTING_URL, params={'page': page}, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = [
            urljoin(LISTING_URL, link['href'])
            for link in soup.select('a[href*="/fr/saison-"]')
            if clean_text(link).upper() == 'EN SAVOIR PLUS'
        ]
        if not page_urls:
            break
        urls.extend(page_urls)
        next_link = soup.select_one('.pager__item--next a, a[rel="next"]')
        if not next_link:
            break
        page += 1
    return list(dict.fromkeys(urls))


def description_from(soup):
    parts = []
    for selector in ('.section-description', '.bk-programm', '.bk-3-cols'):
        for node in soup.select(selector):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def detail_records(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title = clean_text(soup.select_one('main h1'))
    if not title:
        return []
    description = description_from(soup)
    records = []
    for occurrence in soup.select('.bk-event-small li.horizontal-list-item'):
        event_date = parse_date(occurrence.select_one('.date'))
        details = occurrence.select('dd')
        time_text = next((clean_text(node) for node in details if node.select_one('.icon-timer')), '')
        venue = next((clean_text(node) for node in details if node.select_one('.icon-marker')), '')
        venue = re.sub(r'^Adresse\s*', '', venue, flags=re.IGNORECASE).strip()
        city = infer_city(venue)
        if not event_date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(time_text),
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        urls = listing_urls(session)
        records = [record for url in urls for record in detail_records(session, url)]
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Conservatoire de Paris season',
            event='crawler_fetch_failed',
            level='error',
            url=LISTING_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class ConservatoireDeParisFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='conservatoiredeparis_fr',
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
        return get_concerts()


def main():
    ConservatoireDeParisFrCrawler().run()


if __name__ == '__main__':
    main()
