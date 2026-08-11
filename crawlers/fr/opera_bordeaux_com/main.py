import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera-bordeaux.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Opéra National de Bordeaux'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

# The calendar contains touring performances as well as performances in the
# Opera's buildings, so locations are resolved from the venue printed on each
# occurrence rather than applying Bordeaux indiscriminately.
VENUE_CITIES = {
    'Archives départementales': 'Bordeaux',
    'Auditorium': 'Bordeaux',
    'Auditorium - salle Sauguet': 'Bordeaux',
    'Biarritz, La Gare du Midi': 'Biarritz',
    'Cathédrale Saint-André de Bordeaux': 'Bordeaux',
    'Château de Ferrand, Saint-Émilion': 'Saint-Émilion',
    'Grand Théâtre - Foyer gris': 'Bordeaux',
    'Grand Théâtre - Foyer rouge': 'Bordeaux',
    'Grand-Théâtre': 'Bordeaux',
    'Grand-Théâtre, Salon Boireau': 'Bordeaux',
    'Hôtel Frugès, Bordeaux': 'Bordeaux',
    'La Manufacture/CDCN': 'Bordeaux',
    'Le Pin Galant - Mérignac': 'Mérignac',
    "Musée d'Aquitaine": 'Bordeaux',
    'Place de la Comédie': 'Bordeaux',
    'Rocher de Palmer': 'Cenon',
    'Salle des Fêtes Bordeaux Grand Parc': 'Bordeaux',
    'Théâtre Femina': 'Bordeaux',
    'Théâtre Olympia Arcachon': 'Arcachon',
    'Théâtre des 4 saisons Gradignan': 'Gradignan',
    'TnBA, Salle Antoine Vitez': 'Bordeaux',
    'Villa Rem Koolhaas - Floirac': 'Floirac',
    'musba - Musée des Beaux Arts Bordeaux': 'Bordeaux',
}


def make_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(html.unescape(text))
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    url = urljoin(SOURCE_URL, value or '')
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_calendar(html, year, month):
    soup = BeautifulSoup(html, 'html.parser')
    date_slides = soup.select('.swiper-dates .swiper-wrapper > .swiper-slide')
    card_slides = soup.select('.swiper-cartes > .swiper-wrapper > .swiper-slide')
    records = []

    for date_slide, card_slide in zip(date_slides, card_slides):
        date_text = clean_text(date_slide.select_one('.date-heure'))
        match = re.fullmatch(r'(\d{2})/(\d{2})', date_text)
        if not match:
            continue
        day, shown_month = map(int, match.groups())
        shown_year = year
        if shown_month < month - 6:
            shown_year += 1
        elif shown_month > month + 6:
            shown_year -= 1
        try:
            event_date = date(shown_year, shown_month, day).isoformat()
        except ValueError:
            continue

        for card in card_slide.select('.card--calendrier-spectacle'):
            title = clean_text(card.select_one('h4'))
            detail_link = card.select_one('a.stretched-link[href]')
            url = canonical_url(detail_link.get('href') if detail_link else '')
            time_text = clean_text(card.select_one('.date-heure'))
            time_from = time_text if re.fullmatch(r'\d{2}:\d{2}', time_text) else None
            venue = clean_text(card.select_one('.info-icon'))
            city = VENUE_CITIES.get(venue)
            category = clean_text(card.select_one('.category'))
            if not title or not url or url == SOURCE_URL or not venue or not city:
                log_message(
                    'Skipped incomplete Opera Bordeaux calendar item',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url or CALENDAR_URL,
                    error_type='IncompleteEventData',
                    error_message='Required title, URL, venue, or defensible city is missing',
                )
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'FR',
                'description': f'Catégorie: {category}' if category else None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    about = soup.select_one('#tab-0-main .text-long')
    return clean_text(about) or None


def fetch_description(url):
    response = make_session().get(url, timeout=45)
    response.raise_for_status()
    return parse_description(response.text)


class OperaBordeauxComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_bordeaux_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        months = []
        for option in soup.select('select[name="field_date_show_date_value"] option[value]'):
            value = option.get('value', '')
            if re.fullmatch(r'20\d{2}-\d{2}', value):
                months.append(value)

        records = []
        for value in dict.fromkeys(months):
            year, month = map(int, value.split('-'))
            page = session.get(
                CALENDAR_URL,
                params={
                    'field_date_show_date_value': value,
                    'term_node_tid_depth': 'All',
                    'term_node_tid_depth_1': 'All',
                    'field_accessibilite_value': 'All',
                    'field_echelle_de_prix_target_id': 'All',
                },
                timeout=45,
            )
            page.raise_for_status()
            records.extend(parse_calendar(page.text, year, month))

        unique_records = {}
        for record in records:
            key = (record['title'], record['date'], record['time_from'], record['venue'], record['city'])
            unique_records[key] = record
        records = list(unique_records.values())

        descriptions = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_description, url): url for url in {r['url'] for r in records}}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Opera Bordeaux event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            detail = descriptions.get(record['url'])
            if detail:
                record['description'] = '\n\n'.join(
                    part for part in (record['description'], detail) if part
                )
        return sorted(records, key=lambda r: (r['date'], r['time_from'] or '', r['title'], r['venue']))


def main():
    OperaBordeauxComCrawler().run()


if __name__ == '__main__':
    main()
