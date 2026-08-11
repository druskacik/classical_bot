import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.les-surprises.fr/'
AGENDA_URLS = [urljoin(SOURCE_URL, 'agenda-concerts/'), urljoin(SOURCE_URL, 'concerts-passes/')]
PROGRAMMES_URL = urljoin(SOURCE_URL, 'programmes/')
SOURCE = 'Les Surprises'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)',
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janv': 1, 'janvier': 1, 'fev': 2, 'fevr': 2, 'fevrier': 2,
    'mars': 3, 'avr': 4, 'avril': 4, 'mai': 5, 'juin': 6,
    'juil': 7, 'juillet': 7, 'aout': 8, 'sept': 9, 'septembre': 9,
    'oct': 10, 'octobre': 10, 'nov': 11, 'novembre': 11,
    'dec': 12, 'decembre': 12,
}

# The calendar often names a well-known institution instead of repeating its city.
# These are first-party venue strings observed in the current calendar and archive.
CITY_ALIASES = {
    'opera national de bordeaux': 'Bordeaux', 'opera de bordeaux': 'Bordeaux',
    'cathedrale de bordeaux': 'Bordeaux', 'cathedrale saint-andre': 'Bordeaux',
    'opera de limoges': 'Limoges', 'opera de reims': 'Reims',
    'opera de lille': 'Lille', 'opera de massy': 'Massy',
    'opera de clermont-ferrand': 'Clermont-Ferrand', 'clermont-auvergne opera': 'Clermont-Ferrand',
    'theatre des champs-elysees': 'Paris', 'auditorium du louvre': 'Paris',
    'philharmonie de paris': 'Paris', 'maison de la radio': 'Paris',
    'chapelle des invalides': 'Paris', 'salle cortot': 'Paris',
    'bouffes du nord': 'Paris', 'arsenal': 'Metz', 'grand manege': 'Namur',
    'atelier lyrique de tourcoing': 'Tourcoing', 'tap, poitiers': 'Poitiers',
    'montierneuf': 'Poitiers', 'theatre de roanne': 'Roanne',
    'theatre de poissy': 'Poissy', 'nouveau theatre, chatellerault': 'Châtellerault',
    'theatre municipal, langres': 'Langres', 'theatre municipal d’herblay': 'Herblay-sur-Seine',
}

COUNTRIES = {
    'belgique': 'BE', '(be)': 'BE', 'italie': 'IT', 'espagne': 'ES', 'suisse': 'CH',
    'allemagne': 'DE', 'hongrie': 'HU', 'republique tcheque': 'CZ',
    'singapour': 'SG', 'liban': 'LB', 'londres': 'GB', 'montreal': 'CA',
    'bayreuth': 'DE', 'bruges': 'BE', 'bruxelles': 'BE', 'louvain': 'BE',
    'namur': 'BE', 'gent': 'BE', 'malaga': 'ES', 'milan': 'IT', 'milano': 'IT',
    'fribourg': 'CH', 'prague': 'CZ', 'vac': 'HU', 'essen': 'DE', 'augsburg': 'DE',
}

NON_EVENTS = re.compile(r'\b(?:residence|week-end d.?immersion)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def normalized(value):
    value = unicodedata.normalize('NFKD', clean_text(value)).encode('ascii', 'ignore').decode()
    return value.lower().replace('’', "'")


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_dates(value):
    text = normalized(value).replace('1er', '1')
    year_match = re.search(r'\b(20\d{2})\b', text)
    month_match = re.search(r'\b(' + '|'.join(MONTHS) + r')\b', text)
    if not year_match or not month_match:
        return []
    year, month = int(year_match.group(1)), MONTHS[month_match.group(1)]
    prefix = text[:month_match.start()]
    # Enumerated occurrences ("17, 18 & 19") are expanded. Date ranges are
    # deliberately not expanded because they are usually residencies, not concerts.
    if re.search(r'\b(?:du|au)\b', prefix):
        return []
    days = [int(day) for day in re.findall(r'\b(?:lun|mar|mer|jeu|ven|vend|sam|dim)?\s*(\d{1,2})\b', prefix)]
    results = []
    for day in dict.fromkeys(days):
        try:
            results.append(date(year, month, day).isoformat())
        except ValueError:
            pass
    return results


def infer_place(value):
    raw = clean_text(value)
    text = normalized(raw)
    country_code = 'FR'
    for marker, code in COUNTRIES.items():
        if marker in text:
            country_code = code
            break
    for marker, city in CITY_ALIASES.items():
        if marker in text:
            return city, country_code

    parts = [clean_text(part) for part in raw.split(',')]
    ignored = re.compile(r'^(?:france|belgique|italie|espagne|suisse|allemagne|liban|hongrie|québec)$', re.I)
    for part in reversed(parts[1:]):
        part = re.sub(r'\s*\([^)]*\)\s*\.?$', '', part).strip(' .')
        if part and not ignored.match(part) and not re.match(r'^(?:festival|saison|région)\b', part, re.I):
            return part, country_code

    # Common first-party formulations that embed the locality in a venue name.
    match = re.search(r'\b(?:de|du|à)\s+([A-ZÀ-ÖØ-Ý][\wÀ-ÿ’\'-]+(?:[ -][A-ZÀ-ÖØ-Ý][\wÀ-ÿ’\'-]+)*)\s*$', raw)
    return (match.group(1), country_code) if match else (None, country_code)


def programme_descriptions(session):
    soup = get_soup(session, PROGRAMMES_URL)
    links = {}
    for link in soup.select('main a[href*="/programme/"]'):
        title = normalized(link.get_text(' ', strip=True))
        if title:
            links.setdefault(title, urljoin(SOURCE_URL, link.get('href')))

    descriptions = {}
    for title, url in links.items():
        try:
            detail = get_soup(session, url)
            main = detail.select_one('main')
            text = clean_text(main.get_text('\n', strip=True)) if main else ''
            descriptions[title] = text or None
        except requests.RequestException as error:
            log_message('Failed to scrape programme detail', event='crawler_item_failed',
                        level='warning', url=url, error_type=type(error).__name__,
                        error_message=str(error))
    return descriptions


def matching_description(title, descriptions):
    key = normalized(re.sub(r'^(?:annulé\s*[-–]\s*|création\s*[-–]\s*)', '', title, flags=re.I))
    candidates = [(name, text) for name, text in descriptions.items() if name in key or key in name]
    return max(candidates, key=lambda item: len(item[0]))[1] if candidates else None


class LesSurprisesCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='les_surprises_fr', source=SOURCE, source_url=SOURCE_URL,
        country_code='FR', upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        descriptions = programme_descriptions(session)
        records = []
        for page_url in AGENDA_URLS:
            soup = get_soup(session, page_url)
            for article in soup.select('main article.category-events, main article'):
                heading = article.select_one('h2.entry-title, h2')
                blocks = article.select('.fusion-text')
                if not heading or len(blocks) < 2:
                    continue
                title = clean_text(heading.get_text(' ', strip=True))
                if NON_EVENTS.search(title):
                    continue
                dates = parse_dates(blocks[0].get_text(' ', strip=True))
                venue = clean_text(blocks[1].get_text(' ', strip=True))
                city, country_code = infer_place(venue)
                if not dates or not venue or not city:
                    continue
                link = blocks[1].select_one('a[href]')
                url = urljoin(page_url, link.get('href')) if link else f'{page_url}#{article.get("id", "concert")}'
                for event_date in dates:
                    records.append({
                        'title': title, 'date': event_date, 'url': url, 'time_from': None,
                        'venue': venue, 'city': city, 'country_code': country_code,
                        'description': matching_description(title, descriptions),
                    })
        return sorted(records, key=lambda row: (row['date'], row['title'], row['venue']))


def main():
    LesSurprisesCrawler().run()


if __name__ == '__main__':
    main()
