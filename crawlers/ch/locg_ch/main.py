import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import parse_qs, unquote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://locg.ch/'
SOURCE = "L'Orchestre de Chambre de Genève"
CALENDAR_URL = urljoin(SOURCE_URL, 'fr/3')
SEASONS = ('23-24', 'saison-24-25', 'saison-25-26', 'saison-26-27')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CH,fr;q=0.9',
}
MONTHS = {
    'janv': 1, 'janvier': 1, 'févr': 2, 'fevr': 2, 'février': 2,
    'fevrier': 2, 'mars': 3, 'avr': 4, 'avril': 4, 'mai': 5,
    'juin': 6, 'juil': 7, 'juillet': 7, 'août': 8, 'aout': 8,
    'sept': 9, 'septembre': 9, 'oct': 10, 'octobre': 10,
    'nov': 11, 'novembre': 11, 'déc': 12, 'dec': 12,
    'décembre': 12, 'decembre': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    normalized = value.lower().replace('.', '')
    match = re.search(
        r'(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s+'
        r'([a-zàâäéèêëîïôöùûüç]+)\s+(20\d{2})',
        normalized,
    )
    if not match:
        return []
    month = MONTHS.get(match.group(3))
    if not month:
        return []
    try:
        first = date(int(match.group(4)), month, int(match.group(1)))
        last = date(int(match.group(4)), month, int(match.group(2) or match.group(1)))
    except ValueError:
        return []
    if last < first or (last - first).days > 14:
        return []
    return [(first + timedelta(days=offset)).isoformat()
            for offset in range((last - first).days + 1)]


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])h([0-5]\d)?\b', value.lower())
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_map_location(soup):
    link = soup.select_one('a[href*="maps.google.com/maps?q="]')
    if link is None:
        return None
    query = parse_qs(urlparse(link.get('href', '')).query).get('q', [''])[0]
    address = unquote_plus(query).replace('\u2028', ',')
    parts = [part.strip() for part in address.split(',') if part.strip()]
    country_code = 'FR' if any('france' in part.lower() for part in parts) else 'CH'
    for part in reversed(parts):
        match = re.search(r'\b\d{4,5}\s+(.+)$', part)
        if match:
            city = match.group(1).strip()
            if city:
                return city, country_code
    return None


def parse_card(card):
    title_element = card.select_one('h3')
    link = card.select_one('a[href*="/fr/calendrier/"][href]')
    date_element = card.select_one('.text-xl.font-bold')
    venue_element = card.select_one('.text-xl.italic')
    title = clean_text(title_element)
    venue = clean_text(venue_element)
    raw_date = clean_text(date_element)
    if not title or not link or not parse_dates(raw_date):
        return None
    return {
        'title': title,
        'dates': parse_dates(raw_date),
        'time_from': parse_time(raw_date),
        'venue': venue,
        'url': urljoin(SOURCE_URL, link['href']),
    }


def parse_detail(soup):
    location = parse_map_location(soup)
    practical = soup.select_one('a[href*="maps.google.com/maps?q="]')
    practical = practical.find_parent('div', class_='space-y-2') if practical else None
    detail_venue = clean_text(practical.select_one('.font-bold')) if practical else ''
    programme = next(
        (heading for heading in soup.select('h2')
         if clean_text(heading).lower() == 'programme'),
        None,
    )
    description = clean_text(programme.parent) if programme else None
    return location, detail_venue, description or None


class LocgChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='locg_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        cards_by_url = {}
        try:
            for season in SEASONS:
                response = session.get(CALENDAR_URL, params={'saison': season}, timeout=45)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                for card in soup.select('div.group'):
                    parsed = parse_card(card)
                    if parsed:
                        cards_by_url[parsed['url']] = parsed
        except requests.RequestException as error:
            log_message(
                'Failed to fetch LOCG calendar',
                event='crawler_fetch_failed', level='error', url=CALENDAR_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        def fetch_detail(item):
            response = session.get(item['url'], timeout=45)
            response.raise_for_status()
            return item, parse_detail(BeautifulSoup(response.text, 'html.parser'))

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(fetch_detail, item) for item in cards_by_url.values()]
            for future in as_completed(futures):
                try:
                    item, (location, detail_venue, description) = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch LOCG concert detail',
                        event='crawler_fetch_failed', level='warning',
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                venue = item['venue'] or detail_venue
                if not venue:
                    continue
                # Some archived pages retain only an empty Swiss map query. In
                # that case the orchestra's Geneva base is the defensible
                # default; touring pages carry a real map address and never use
                # this fallback.
                city, country_code = location or ('Genève', 'CH')
                for event_date in item['dates']:
                    records.append({
                        'title': item['title'], 'date': event_date, 'url': item['url'],
                        'time_from': item['time_from'], 'venue': venue,
                        'city': city, 'country_code': country_code,
                        'description': description, 'source_url': SOURCE_URL,
                        'source': SOURCE,
                    })

        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ))


def main():
    LocgChCrawler().run()


if __name__ == '__main__':
    main()
