import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera-orchestre-montpellier.fr/'
SOURCE = 'Opéra Orchestre National Montpellier Occitanie'
ARCHIVES_URL = urljoin(SOURCE_URL, 'opera-orchestre/archives/')
SEASON_URLS = (
    urljoin(SOURCE_URL, 'saison-26-27/saison-2026-2027/'),
    urljoin(SOURCE_URL, 'saison-25-26/saison-2025-2026/'),
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def normalized(value):
    return ''.join(
        character for character in unicodedata.normalize('NFD', clean_text(value).lower())
        if unicodedata.category(character) != 'Mn'
    )


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def season_years(url):
    match = re.search(r'(20\d{2})-(20\d{2})', url)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r'(20\d{2})-(\d{2})(?:/|$)', url)
    if match:
        return int(match.group(1)), int(match.group(1)[:2] + match.group(2))
    return None


def available_season_urls(session):
    soup = get_soup(session, ARCHIVES_URL)
    urls = set(SEASON_URLS)
    for link in soup.select('a[href]'):
        url = canonical_url(link.get('href'))
        if season_years(url) and ('/archives/' in url or '/saison-' in url):
            urls.add(url)
    return sorted(urls)


def parse_listing_location(value):
    parts = [part.strip() for part in clean_text(value).split('|') if part.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return 'Montpellier', parts[0]

    first = normalized(parts[0])
    venue_words = (
        'opera', 'salle', 'theatre', 'eglise', 'cathedrale', 'chapelle',
        'auditorium', 'conservatoire', 'domaine', 'cour ', 'grand foyer',
        'cite internationale', 'le corum', 'zenith', 'musee',
    )
    if any(word in first for word in venue_words):
        return 'Montpellier', ' | '.join(parts)
    return parts[0], ' | '.join(parts[1:])


def listing_items(soup, years):
    items = {}
    for card in soup.select('a.bloc-spectacle[href*="/evenements/"]'):
        url = canonical_url(card.get('href'))
        title = clean_text(card.select_one('.bloc-spectacle-infos h2'))
        location = parse_listing_location(card.select_one('.bloc-spectacle-infos-lieu'))
        if url and title and location:
            items[url] = {'title': title, 'city': location[0], 'venue': location[1], 'years': years}
    return items


def parse_occurrence(node, years):
    paragraphs = node.find_all('p')
    date_text = clean_text(paragraphs[0] if paragraphs else node)
    match = re.search(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)', date_text)
    if not match:
        return None
    month = MONTHS.get(normalized(match.group(2)))
    if not month:
        return None
    start_year, end_year = years
    year = start_year if month >= 8 else end_year
    try:
        event_date = date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None
    time_match = re.search(r'\b([01]?\d|2[0-3])h([0-5]\d)\b', clean_text(node))
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, time_from


def detail_records(session, url, listing):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('main h1')) or listing['title']
    description_node = soup.select_one('.section-spectacle-description')
    if description_node:
        for unwanted in description_node.select('button.share, button.share + div'):
            unwanted.decompose()
    description = clean_text(description_node, separator='\n') or None
    records = []
    for occurrence in soup.select('.section-spectacle-dates-date'):
        parsed = parse_occurrence(occurrence, listing['years'])
        if not parsed:
            continue
        records.append({
            'title': title,
            'date': parsed[0],
            'url': url,
            'time_from': parsed[1],
            'venue': listing['venue'],
            'city': listing['city'],
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class OperaOrchestreMontpellierFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_orchestre_montpellier_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        listings = {}
        for season_url in available_season_urls(session):
            years = season_years(season_url)
            if not years:
                continue
            try:
                listings.update(listing_items(get_soup(session, season_url), years))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Montpellier season page',
                    event='crawler_page_failed', level='warning', url=season_url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(detail_records, session, url, listing): url
                for url, listing in listings.items()
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Montpellier event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    OperaOrchestreMontpellierFrCrawler().run()


if __name__ == '__main__':
    main()
