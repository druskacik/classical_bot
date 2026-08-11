import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ensemblecorrespondances.com/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts/')
CONCERTS_API = urljoin(SOURCE_URL, 'wp-json/wp/v2/concert')
SOURCE = 'Ensemble Correspondances'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'janv': 1, 'janvier': 1,
    'fev': 2, 'fevr': 2, 'fevrier': 2,
    'mar': 3, 'mars': 3,
    'avr': 4, 'avril': 4,
    'mai': 5,
    'jun': 6, 'juin': 6,
    'jul': 7, 'juil': 7, 'juillet': 7,
    'aou': 8, 'aout': 8,
    'sep': 9, 'sept': 9, 'septembre': 9,
    'oct': 10, 'octobre': 10,
    'nov': 11, 'novembre': 11,
    'dec': 12, 'decembre': 12,
}

COUNTRIES = {
    'allemagne': 'DE', 'autriche': 'AT', 'belgique': 'BE',
    'canada': 'CA', 'chine': 'CN', 'espagne': 'ES', 'etats-unis': 'US',
    'italie': 'IT', 'luxembourg': 'LU', 'pays-bas': 'NL',
    'pologne': 'PL', 'portugal': 'PT', 'royaume-uni': 'GB',
    'suisse': 'CH', 'tchequie': 'CZ',
}

# These place-taxonomy labels name an institution rather than spelling out its
# locality. They are first-party labels repeatedly used by the concert archive.
PLACE_GEOGRAPHY = {
    'academie bach de dieppe': ('Dieppe', 'FR'),
    'abbaye de la lucerne': ('La Lucerne-d’Outremer', 'FR'),
    'centro nacional de difusion musical': ('Madrid', 'ES'),
    'boston early music festival (etats-unis)': ('Boston', 'US'),
    'festival musique sacree de la chaise-dieu': ('La Chaise-Dieu', 'FR'),
    "festival promenades musicales du pays d'auge": ('Lisieux', 'FR'),
    "promenades musicales du pays d'auge": ('Lisieux', 'FR'),
    'sainte-chapelle': ('Paris', 'FR'),
    'san diego early music society (etats-unis)': ('San Diego', 'US'),
    'san francisco early music society (etats-unis)': ('San Francisco', 'US'),
    'theatre des champs-elysees': ('Paris', 'FR'),
    'washington library of congress (etats-unis)': ('Washington', 'US'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def folded(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value))
        if not unicodedata.combining(character)
    ).casefold().replace('’', "'")


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, clean_text(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\b', clean_text(value))
    if not match:
        return None
    month = MONTHS.get(folded(match.group(2)).rstrip('.')[:4])
    if not month:
        month = MONTHS.get(folded(match.group(2)).rstrip('.'))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def resolve_geography(venue):
    key = folded(venue)
    if key in PLACE_GEOGRAPHY:
        return PLACE_GEOGRAPHY[key]

    country_code = 'FR'
    for label, code in COUNTRIES.items():
        if label in key:
            country_code = code
            break

    without_country = re.sub(r'\s*\([^)]*\)\s*$', '', clean_text(venue)).strip()
    # The site commonly stores "physical venue, locality" as one place term.
    if ',' in without_country:
        city = without_country.rsplit(',', 1)[1].strip()
        if city and not re.search(r'\d', city):
            return city, country_code

    # Otherwise its place terms usually end in an explicit locality: Théâtre
    # de Caen, Château de Versailles, Festival de Saintes, ... .
    matches = list(re.finditer(r"\b(?:a|de|du|des|d')\s+([A-ZÀ-ÖØ-Ý][\wÀ-ÿ'’.-]*(?:[- ](?:sur|sous|en|les|la|le|du|de|d')[- ]?[A-ZÀ-ÖØ-Ý][\wÀ-ÿ'’.-]*)*)$", without_country))
    if matches:
        city = matches[-1].group(1).strip()
        if len(city) >= 3:
            return city, country_code

    # Foreign festival labels often include the city immediately before the
    # parenthesized country, without a preposition.
    if country_code != 'FR':
        tokens = re.findall(r"[A-ZÀ-ÖØ-Ý][\wÀ-ÿ'’.-]+", without_country)
        if tokens:
            return tokens[-1], country_code
    return None, None


def parse_card(card):
    title = clean_text(card.select_one('h3'))
    event_date = parse_date(card.select_one('time'))
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(card.select_one('time')))
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    venue = clean_text(card.select_one('p'))
    url = canonical_url(card.get('href'))
    city, country_code = resolve_geography(venue)
    if not all((title, event_date, url, venue, city, country_code)):
        return None
    return {
        'title': title, 'date': event_date, 'url': url, 'time_from': time_from,
        'venue': venue, 'city': city, 'country_code': country_code,
        'description': None, 'source_url': SOURCE_URL, 'source': SOURCE,
    }


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('main article.o-wysiwyg')
    if not article:
        return None
    header = article.select_one(':scope > header')
    title = clean_text(header.select_one('h1')) if header else ''
    date_line = clean_text(header.select_one('.u-h3')) if header else ''
    venue = clean_text(header.select_one('p')) if header else ''
    event_date = parse_date(date_line)
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', date_line)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    city, country_code = resolve_geography(venue)
    if not all((title, event_date, venue, city, country_code)):
        return None
    header = article.select_one(':scope > header')
    if header:
        header.decompose()
    for node in article.select('.archive-link, script, style'):
        node.decompose()
    return {
        'title': title, 'date': event_date, 'url': url, 'time_from': time_from,
        'venue': venue, 'city': city, 'country_code': country_code,
        'description': clean_text(article) or None,
        'source_url': SOURCE_URL, 'source': SOURCE,
    }


def fetch_detail(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_detail(response.content, url)


def api_urls(session, past_events=False):
    urls = []
    page = 1
    while True:
        params = {'page': page, 'per_page': 100, '_fields': 'link'}
        if past_events:
            params['past_events'] = 'true'
        response = session.get(
            CONCERTS_API,
            params=params,
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        urls.extend(canonical_url(item.get('link')) for item in batch if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages or not batch:
            break
        page += 1
    return urls


def listing_records(session, start_url):
    records = []
    page_url = start_url
    seen_pages = set()
    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for card in soup.select('main a.c-card[data-type="concert"]'):
            record = parse_card(card)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Ensemble Correspondances concert',
                    event='crawler_item_skipped', level='warning',
                    url=canonical_url(card.get('href')) or page_url,
                    error_type='IncompleteEventData',
                    error_message='Required date, venue, or defensible city is missing',
                )
        next_link = soup.select_one('.c-pagination a.next')
        page_url = canonical_url(next_link.get('href')) if next_link else None
    return records


class EnsembleCorrespondancesComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensemblecorrespondances_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = set(api_urls(session))
        urls.update(api_urls(session, past_events=True))
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_detail, session, url): url for url in urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Ensemble Correspondances concert',
                            event='crawler_item_skipped', level='warning', url=url,
                            error_type='IncompleteEventData',
                            error_message='Required date, venue, or defensible city is missing',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Ensemble Correspondances concert detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue'],
        ))


def main():
    EnsembleCorrespondancesComCrawler().run()


if __name__ == '__main__':
    main()
