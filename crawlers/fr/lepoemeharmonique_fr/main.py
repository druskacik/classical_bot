import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lepoemeharmonique.fr/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda')
SOURCE = 'Le Poème Harmonique'

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

COUNTRIES = {
    'allemagne': 'DE', 'autriche': 'AT', 'belgique': 'BE',
    'coree du sud': 'KR', 'espagne': 'ES', 'italie': 'IT',
    'mexique': 'MX', 'pays-bas': 'NL', 'pologne': 'PL',
    'portugal': 'PT', 'royaume-uni': 'GB', 'suisse': 'CH',
}

# A few venue names on the agenda contain their city instead of separating it
# with a comma. These are explicit place-name inferences, not home-venue defaults.
VENUE_CITIES = {
    "abbaye de longues-sur-mer": 'Longues-sur-Mer',
    "grange de l'abbaye de lessay": 'Lessay',
    'abbaye de lessay': 'Lessay',
    "basilique d'alencon": 'Alençon',
    'le mont saint-michel': 'Le Mont-Saint-Michel',
    'chapelle royale de versailles': 'Versailles',
    'theatre de coutances': 'Coutances',
    'centre culturel malesherbes': 'Maisons-Laffitte',
    'sala argenta de santander': 'Santander',
    "palais de l'europe": 'Menton',
    "abbaye notre-dame d'ambronay": 'Ambronay',
    'chateau de sceaux': 'Sceaux',
    'theatre de poissy': 'Poissy',
}


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
        character for character in unicodedata.normalize('NFKD', clean_text(value).lower())
        if not unicodedata.combining(character)
    )


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, clean_text(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_date_and_times(value):
    text = clean_text(value)
    match = re.search(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})', text)
    if not match:
        return None, []
    month = MONTHS.get(normalized(match.group(2)))
    if not month:
        return None, []
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, []
    times = []
    for hour, minute in re.findall(r'(?<!\d)([01]?\d|2[0-3])h([0-5]\d)', text):
        event_time = f'{int(hour):02d}:{minute}'
        if event_time not in times:
            times.append(event_time)
    return event_date, times or [None]


def parse_location(value):
    lines = clean_text(value).split('\n')
    first_line = lines[0]
    if len(lines) > 1 and re.fullmatch(r'\([^()]+\)', lines[1]):
        first_line += f' {lines[1]}'
    country_code = 'FR'
    country_match = re.search(r'\(([^()]+)\)\s*$', first_line)
    if country_match:
        label = normalized(country_match.group(1))
        if not label.isdigit():
            country_code = COUNTRIES.get(label, '')
            if not country_code:
                return '', '', ''
        first_line = first_line[:country_match.start()].strip(' ,')

    if ',' in first_line:
        venue, city = (part.strip() for part in first_line.rsplit(',', 1))
    else:
        venue = first_line
        city = VENUE_CITIES.get(normalized(venue), '')
    if len(lines) > 1 and re.match(r'(?i)sal(?:a|le)\b', lines[1]):
        venue = lines[1]
    return venue, city, country_code


def find_cards(soup):
    cards = []
    seen = set()
    date_pattern = re.compile(r'\b\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+20\d{2}\b')
    for text_node in soup.find_all(string=date_pattern):
        card = text_node.find_parent('div', class_='wixui-column-strip__column')
        if card is not None and id(card) not in seen:
            seen.add(id(card))
            cards.append(card)
    return cards


def parse_card(card):
    fields = [clean_text(element) for element in card.select('.wixui-rich-text')]
    fields = [field for field in fields if field]
    date_index = next((i for i, field in enumerate(fields) if parse_date_and_times(field)[0]), None)
    if date_index is None or date_index == 0 or date_index + 1 >= len(fields):
        return []

    title = fields[0].replace('\n', ' ').strip()
    subtitle = '\n'.join(fields[1:date_index])
    event_date, times = parse_date_and_times(fields[date_index])
    venue, city, country_code = parse_location(fields[date_index + 1])
    detail_link = next(
        (link.get('href') for link in card.select('a[href]')
         if 'lepoemeharmonique.fr/' in urljoin(SOURCE_URL, link.get('href', ''))),
        None,
    )
    url = canonical_url(detail_link or AGENDA_URL)
    if not all((title, event_date, url, venue, city, country_code)):
        return []

    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': subtitle or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_time in times]


def fetch_description(url):
    if url == canonical_url(AGENDA_URL):
        return None
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return clean_text(soup.find('main')) or None


class LePoemeHarmoniqueFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lepoemeharmonique_fr',
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
        response = requests.get(AGENDA_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        records = []
        for card in find_cards(soup):
            parsed = parse_card(card)
            if parsed:
                records.extend(parsed)
            else:
                log_message(
                    'Skipped incomplete Le Poème Harmonique event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=AGENDA_URL,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, city, or country is missing',
                )

        descriptions = {}
        urls = sorted({record['url'] for record in records})
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_description, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Le Poème Harmonique programme page',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url']) or record['description']
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    LePoemeHarmoniqueFrCrawler().run()


if __name__ == '__main__':
    main()
