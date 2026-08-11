import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://jacquesmoderne.com/'
SOURCE = 'Ensemble Jacques Moderne'
LIST_URLS = (urljoin(SOURCE_URL, 'fr/agenda'), urljoin(SOURCE_URL, 'fr/archives'))
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

COUNTRY_MARKERS = {
    'COLOMBIE': 'CO',
    'SUISSE': 'CH',
    'CANADA': 'CA',
    'ESPAGNE': 'ES',
    'JAPON': 'JP',
    'MONACO': 'MC',
}
MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'decembre': 12,
}
VENUE_WORDS = re.compile(
    r"\b(?:eglise|cathedrale|abbatiale|abbaye|chapelle|basilique|collegiale|"
    r"theatre|opera|auditorium|oratoire|temple|chateau|domaine|palais|salle|"
    r"conservatoire|musee|prieure|grange|centre culturel|hotel|espace|college)\b",
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value))
        if not unicodedata.combining(character)
    )


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def event_urls():
    urls = set()
    for list_url in LIST_URLS:
        soup = get_soup(list_url)
        urls.update(
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select('a.pg-evenement-lien[href]')
        )
    return sorted(urls)


def parse_date(value):
    match = re.search(
        r'\b(\d{1,2})\s+(janvier|f[ée]vrier|mars|avril|mai|juin|juillet|'
        r'ao[uû]t|septembre|octobre|novembre|d[ée]cembre)\s+(\d{4})\b',
        normalized(value).casefold(),
    )
    if not match:
        return None
    try:
        return datetime(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)?\b', clean_text(value))
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}' if match else None


def city_country(value):
    location = clean_text(value)
    folded = normalized(location).upper()
    country_code = 'FR'
    for marker, code in COUNTRY_MARKERS.items():
        if marker in folded:
            country_code = code
            location = re.sub(rf'\s*,?\s*{marker}\s*$', '', location, flags=re.IGNORECASE)
            break
    location = re.sub(r'\s*\(\d{2,3}\)\s*$', '', location).strip(' ,-')
    # Some French entries prefix a landmark before the municipality.
    if ',' not in location and 'PARC DE LA GLORIETTE' in normalized(location).upper():
        location = location.rsplit(' ', 1)[-1]
    return location.title(), country_code


def choose_venue(soup, subtitle, city):
    body = soup.select_one('#node-texte')
    if body:
        for line in clean_text(body).splitlines():
            candidate = line.strip(' -|')
            if VENUE_WORDS.search(normalized(candidate)) and len(candidate) <= 180:
                return candidate
    subtitle = clean_text(subtitle)
    folded = normalized(subtitle).casefold()
    if (
        subtitle
        and folded != normalized(city).casefold()
        and not folded.startswith('festival ')
    ):
        return subtitle
    return None


def detail(url):
    soup = get_soup(url)
    title = clean_text(soup.select_one('h1.node-titre'))
    date_text = clean_text(soup.select_one('.node-date-affichage'))
    event_date = parse_date(date_text)
    location = clean_text(soup.select_one('.node-lieu-affichage'))
    city, country_code = city_country(location)
    venue = choose_venue(soup, soup.select_one('.node-sous-titre'), city)
    description = clean_text(soup.select_one('#node-texte')) or None
    programme = soup.select_one('a.programme[href]')
    programme_url = urljoin(SOURCE_URL, programme['href']) if programme else None
    if not title or not event_date or not city or not venue:
        return None, programme_url
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_text) or parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'programme_url': programme_url,
    }, programme_url


def fetch_details(urls):
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record, _ = future.result()
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Jacques Moderne event detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    return records


def programme_description(url):
    soup = get_soup(url)
    return clean_text(soup.select_one('#node-texte')) or None


def enrich_programmes(records):
    urls = {record['programme_url'] for record in records if record['programme_url']}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(programme_description, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Jacques Moderne programme detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    for record in records:
        programme = descriptions.get(record.pop('programme_url'))
        if programme:
            record['description'] = '\n\n'.join(
                part for part in (record['description'], programme) if part
            )


class JacquesModerneCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jacquesmoderne_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        records = fetch_details(event_urls())
        enrich_programmes(records)
        return sorted(records, key=lambda record: (record['date'], record['time_from'] or '', record['url']))


def main():
    return JacquesModerneCrawler().run()


if __name__ == '__main__':
    main()
