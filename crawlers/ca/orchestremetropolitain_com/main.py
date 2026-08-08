import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestremetropolitain.com/fr/'
ARCHIVE_URL = f'{SOURCE_URL}saisons/tous/'
SOURCE = 'Orchestre Métropolitain'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CA,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
}

# The calendar identifies venues but omits their municipality. These stable
# venue-to-city facts cover every physical venue currently present in its
# published seasons. Montreal borough names are intentionally normalized to
# the municipality of Montréal.
VENUE_CITIES = {
    'Amphithéâtre Cogeco (Trois-Rivières)': 'Trois-Rivières',
    'Amphithéâtre Fernand-Lindsay': 'Joliette',
    'Au pied du mont Royal': 'Montréal',
    'Basilique Notre-Dame de Montréal': 'Montréal',
    'Centre Notre-Dame (Granby)': 'Granby',
    'Centre culturel Desjardins (Société musicale Fernand-Lindsay - Opus 130': 'Joliette',
    'Domaine Forget de Charlevoix': 'Saint-Irénée',
    'Festival de Lanaudière': 'Joliette',
    'Festival des Arts de Saint-Sauveur': 'Saint-Sauveur',
    'Festival des arts de St-Sauveur': 'Saint-Sauveur',
    'Maison Symphonique de Montréal': 'Montréal',
    'Maison de la Culture Ahuntsic (Salle Marguerite-Bourgeoys)': 'Montréal',
    'Maison de la culture Mercier': 'Montréal',
    'Parc Ahuntsic (Ahuntsic-Cartierville)': 'Montréal',
    'Parc Armand-Bombardier (RDP-PAT)': 'Montréal',
    'Parc Daniel-Johnson (Granby)': 'Granby',
    'Parc LaSalle (Lachine)': 'Montréal',
    'Parc Marie-Claire-Kirkland-Casgrain (Lasalle)': 'Montréal',
    'Parc Pilon (Montréal-Nord)': 'Montréal',
    'Parc Saint-Jean-Baptiste (RDP-PAT)': 'Montréal',
    'Parc Wilfrid-Bastien (Saint-Léonard)': 'Montréal',
    'Parc de West-Vancouver, L’Île-des-Soeurs': 'Montréal',
    'SPEC, Théâtre des Deux Rives (Saint-Jean-sur-Richelieu)': 'Saint-Jean-sur-Richelieu',
    'Salle Bourgie': 'Montréal',
    'Salle Désilets (Rivière-des-Prairies–Pointe-aux-Trembles)': 'Montréal',
    'Salle Jean-Eudes (Rosemont–La Petite-Patrie)': 'Montréal',
    'Salle Louis-Fréchette (Québec)': 'Québec',
    'Salle Maurice-O’Bready (Sherbrooke)': 'Sherbrooke',
    'Salle Pauline-Julien (L’île-Bizard — Sainte-Geneviève)': 'Montréal',
    'Salle Pierre-Mercure, Centre Péladeau de l’UQAM': 'Montréal',
    'Salle Wilfrid-Pelletier (Montréal)': 'Montréal',
    'Théâtre Desjardins (LaSalle)': 'Montréal',
    'Théâtre Mirella et Lino Saputo (Saint-Léonard)': 'Montréal',
    'Théâtre Outremont': 'Montréal',
    'Théâtre de Verdure (Plateau-Mont-Royal)': 'Montréal',
    'Théâtre de la Ville de Longueuil': 'Longueuil',
    'Théâtre du Marais': 'Val-Morin',
    'Église Notre-Dame-des-Sept-Douleurs (Verdun)': 'Montréal',
    'Église Saint-Esprit-de-Rosemont (Rosemont–La Petite-Patrie)': 'Montréal',
    'Église Saint-Joachim (Pointe-Claire)': 'Pointe-Claire',
    'Église Saint-Sixte (Saint-Laurent)': 'Montréal',
    'Église Sainte-Claire (Mercier)': 'Montréal',
    'Église Sainte-Suzanne (Pierrefonds)': 'Montréal',
    'Église du Très-Saint-Nom-de-Jésus (Hochelaga-Maisonneuve)': 'Montréal',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(value):
    match = re.search(r'(\d{1,2})\s+([a-zéû]+)\s+(\d{4})', value.lower())
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def detail_key(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def listing_record(node):
    link = node.select_one('.show-date-preview__link[href]')
    title_node = node.select_one('.show-date-preview__title')
    venue_node = node.select_one('.show-date-preview__spec .fw-700')
    header = node.select_one('.show-date-preview__header')
    if not link or not title_node or not venue_node or not header:
        return None

    title = clean_text(title_node)
    venue = clean_text(venue_node)
    city = VENUE_CITIES.get(venue)
    event_date = parse_date(clean_text(header))
    time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', clean_text(header))
    url = link.get('href', '').strip()
    if not title or not event_date or not url or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'CA',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(url):
    soup = get_soup(url)
    parts = []
    for selector in (
        '.show-single__header__content',
        '.show-single__header__musicalWorks',
    ):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def season_urls():
    soup = get_soup(ARCHIVE_URL)
    return sorted(
        {
            link['href']
            for link in soup.select('.season-preview a[href]')
            if '/fr/saisons/' in link.get('href', '')
        }
    )


def get_concerts():
    records_by_url = {}
    for url in season_urls():
        try:
            soup = get_soup(url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch season page',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for node in soup.select('.show-date-preview'):
            record = listing_record(node)
            if record:
                records_by_url[record['url']] = record

    descriptions = {}
    detail_urls = {detail_key(record['url']) for record in records_by_url.values()}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_description, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records_by_url.values():
        record['description'] = descriptions.get(detail_key(record['url']))
    return sorted(
        records_by_url.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class OrchestremetropolitainComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestremetropolitain_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
        upload_target='classical',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OrchestremetropolitainComCrawler().run()


if __name__ == '__main__':
    main()
