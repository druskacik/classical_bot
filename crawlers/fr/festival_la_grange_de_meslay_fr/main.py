import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festival-la-grange-de-meslay.fr/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Festival de la Grange de Meslay'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fold(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value).lower())
        if not unicodedata.combining(character)
    )


def canonical_url(value):
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_sitemap(xml):
    soup = BeautifulSoup(xml, 'xml')
    results = []
    for entry in soup.select('url'):
        location = clean_text(entry.select_one('loc'))
        if '/programmation/' not in location:
            continue
        modified = clean_text(entry.select_one('lastmod'))
        fallback_year = int(modified[:4]) if re.match(r'^20\d{2}', modified) else None
        results.append((canonical_url(location), fallback_year))
    return results


def event_year(url, page_title, fallback_year):
    text = f'{url} {page_title}'
    years = [int(value) for value in re.findall(r'(?<!\d)(20\d{2})(?!\d)', text)]
    return years[-1] if years else fallback_year


def parse_event_date(value, year):
    match = re.search(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)', fold(value))
    if not match or not year:
        return None
    month = MONTHS.get(match.group(2))
    if not month:
        return None
    try:
        return date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})\s*h\s*(\d{2})?\b', clean_text(value), re.I)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def infer_location(url, page_title):
    evidence = fold(f'{url} {page_title}')
    if 'domaine-de-cande' in evidence or 'domaine de cande' in evidence:
        return 'Domaine de Candé', 'Monts'
    if 'atrium' in evidence:
        return 'Nouvel Atrium', 'Saint-Avertin'
    if 'theleme' in evidence:
        return 'Salle Thélème', 'Tours'
    if 'jardin-de-la-prefecture' in evidence or 'jardin de la prefecture' in evidence:
        return 'Jardin de la Préfecture', 'Tours'
    if 'moisson' in evidence:
        return 'Grange de Meslay', 'Parçay-Meslay'
    # The retained Soirées Musicales cycles are the festival's first-party
    # concerts at the Salle des fêtes de l'Hôtel de Ville in Tours.
    return "Salle des fêtes de l'Hôtel de Ville", 'Tours'


def build_description(soup):
    parts = []
    works = [clean_text(item) for item in soup.select('.NodeConcertTop-content-work .WorkItem')]
    works = list(dict.fromkeys(item for item in works if item))
    if works:
        parts.append('Programme\n' + '\n'.join(works))
    for interpreter in soup.select('.ParagraphInterpreter'):
        text = clean_text(interpreter)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail(html, url, fallback_year):
    soup = BeautifulSoup(html, 'html.parser')
    title = re.sub(r'\s+', ' ', clean_text(soup.select_one('h1.NodeConcertTop-content-title')))
    date_text = clean_text(soup.select_one('.date-hour .date'))
    time_text = clean_text(soup.select_one('.date-hour .hour'))
    page_title = clean_text(soup.title)
    year = event_year(url, page_title, fallback_year)
    event_date = parse_event_date(date_text, year)
    venue, city = infer_location(url, page_title)
    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_text),
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': build_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(url, fallback_year):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url, fallback_year)


class FestivalLaGrangeDeMeslayFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festival_la_grange_de_meslay_fr',
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
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        entries = parse_sitemap(response.text)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_detail, url, fallback_year): url
                for url, fallback_year in entries
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Grange de Meslay event',
                            event='crawler_item_skipped',
                            level='warning',
                            url=url,
                            error_type='IncompleteEventData',
                            error_message='Required title, date, venue, or city is missing',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Grange de Meslay event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    FestivalLaGrangeDeMeslayFrCrawler().run()


if __name__ == '__main__':
    main()
