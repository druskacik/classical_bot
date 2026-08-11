import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lestalenslyriques.com/'
SOURCE = 'Les Talens Lyriques'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/event')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

COUNTRY_CODES = {
    'Allemagne': 'DE', 'Argentine': 'AR', 'Autriche': 'AT',
    'Belgique': 'BE', 'Brésil': 'BR', 'Canada': 'CA', 'Chine': 'CN',
    'Corée du Sud': 'KR', 'Danemark': 'DK', 'Espagne': 'ES',
    'Etats-Unis': 'US', 'États-Unis': 'US', 'Finlande': 'FI',
    'France': 'FR', 'Grèce': 'GR', 'Hong Kong': 'HK', 'Hongrie': 'HU',
    'Italie': 'IT', 'Japon': 'JP', 'Luxembourg': 'LU', 'Mexique': 'MX',
    'Monaco': 'MC', 'Norvège': 'NO', 'Pays-Bas': 'NL', 'Pérou': 'PE',
    'Pologne': 'PL', 'Portugal': 'PT', 'République tchèque': 'CZ',
    'Roumanie': 'RO', 'Royaume-Uni': 'GB', 'Russie': 'RU',
    'Singapour': 'SG', 'Slovaquie': 'SK', 'Slovénie': 'SI',
    'Suède': 'SE', 'Suisse': 'CH', 'Taïwan': 'TW', 'Turquie': 'TR',
    'Uruguay': 'UY',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_event_index():
    events = []
    page = 1
    while True:
        response = requests.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link',
            },
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return events
        page += 1


def parse_date(card):
    date_element = card.select_one('.date')
    if not date_element:
        return None
    day = date_element.find(string=True, recursive=False)
    month_year = clean_text(date_element.select_one('span'))
    value = f'{clean_text(day)} {month_year}'
    try:
        return datetime.strptime(value, '%d %B %Y').date().isoformat()
    except ValueError:
        months = {
            'jan': 1, 'janv': 1, 'janvier': 1,
            'fév': 2, 'févr': 2, 'février': 2,
            'mar': 3, 'mars': 3, 'avr': 4, 'avril': 4, 'mai': 5,
            'juin': 6, 'juil': 7, 'juillet': 7, 'août': 8,
            'sept': 9, 'septembre': 9, 'oct': 10, 'octobre': 10,
            'nov': 11, 'novembre': 11, 'déc': 12, 'décembre': 12,
        }
        match = re.fullmatch(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})', value)
        if not match or match.group(2).lower() not in months:
            return None
        try:
            return datetime(
                int(match.group(3)), months[match.group(2).lower()], int(match.group(1))
            ).date().isoformat()
        except ValueError:
            return None


def parse_event_page(html, fallback_url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.twTitle'))
    composer = clean_text(soup.select_one('.twSubTitle'))
    description_parts = [composer]
    for selector in ('.blockContent', '.blockDescription'):
        value = clean_text(soup.select_one(selector))
        if value:
            description_parts.append(value)
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None

    records = []
    for card in soup.select('.nextEvent.itemBox'):
        event_date = parse_date(card)
        occurrence_link = card.select_one('a[href]')
        url = urljoin(SOURCE_URL, occurrence_link.get('href')) if occurrence_link else fallback_url
        time_from = clean_text(card.select_one('.time')) or None
        if time_from and not re.fullmatch(r'\d{1,2}:\d{2}', time_from):
            time_from = None
        place_text = clean_text(card.select_one('.place'))
        delimiter = '|' if '|' in place_text else ' / ' if ' / ' in place_text else None
        place_parts = [part.strip() for part in place_text.split(delimiter)] if delimiter else [place_text]
        place_parts = [part for part in place_parts if part]
        country_name = clean_text(card.select_one('.country'))
        country_code = COUNTRY_CODES.get(country_name)
        if not title or not event_date or len(place_parts) < 2 or not country_code:
            log_message(
                'Skipped incomplete Les Talens Lyriques occurrence',
                event='crawler_item_skipped', level='warning', url=url,
                error_type='IncompleteEventData',
                error_message='Required title, date, venue, city, or country is missing',
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': ' | '.join(place_parts[:-1]),
            'city': place_parts[-1],
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_and_parse_event(event):
    url = event['link']
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event_page(response.text, url)


class LesTalensLyriquesCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lestalenslyriques_com',
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
        events = fetch_event_index()
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_and_parse_event, event): event for event in events}
            for future in as_completed(futures):
                event = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Les Talens Lyriques event',
                        event='crawler_item_failed', level='warning', url=event['link'],
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    LesTalensLyriquesCrawler().run()


if __name__ == '__main__':
    main()
