import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lespaladins.com/'
AGENDA_URL = f'{SOURCE_URL}agenda/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/date'
SOURCE = 'Les Paladins'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
    'decembre': 12,
}

COUNTRY_NAMES = {
    'belgique': 'BE', 'espagne': 'ES', 'italie': 'IT', 'pologne': 'PL',
    'portugal': 'PT', 'allemagne': 'DE', 'autriche': 'AT',
    'suisse': 'CH', 'royaume-uni': 'GB', 'angleterre': 'GB',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})', value)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def country_for(location):
    lowered = location.lower()
    for name, code in COUNTRY_NAMES.items():
        if name in lowered:
            return code
    return 'FR'


def parse_city(value):
    text = clean_text(value)
    text = re.sub(r'\(\s*\)', '', text).strip()
    postal = re.search(r'\b\d{4,5}\s+(.+?)(?:\s*\([^)]+\))?$', text)
    if postal:
        return postal.group(1).strip(' ,-')
    foreign = re.search(
        r'^(?:Belgique|Espagne|Italie|Pologne|Portugal|Allemagne|Autriche|Suisse)\s+(.+)$',
        text,
        re.I,
    )
    if foreign:
        return foreign.group(1).strip(' ,-')
    parenthesized = re.match(r'^(.+?)\s*\((?:Belgique|Espagne|Italie|Pologne)\)$', text, re.I)
    if parenthesized:
        return parenthesized.group(1).strip(' ,-')
    text = re.sub(r'^\d{2,3}\s+(?=[A-Za-zÀ-ÿ])', '', text)
    return text.strip(' ,-')


def parse_venue(element):
    if not element:
        return ''
    lines = [clean_text(part) for part in element.stripped_strings]
    lines = [line for line in lines if line]
    if not lines:
        return ''
    venue = lines[-1]
    if len(lines) > 1 and re.match(r'^\d+\s+', venue):
        venue = lines[-2]
    # Detail pages commonly append a street address after a separator.
    venue = re.split(r'\s+[|–]\s+|\s+-\s+(?=\d)', venue, maxsplit=1)[0]
    venue = re.sub(
        r'\s+\d+\s+(?:rue|avenue|boulevard|place|chemin|allée|route)\b.*$',
        '', venue, flags=re.I,
    )
    return venue.strip(' ,-')


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    date_heading = soup.select_one('main h1')
    event_date = parse_date(clean_text(date_heading))
    title_heading = soup.select_one('main h2')
    title = clean_text(title_heading)

    time_from = None
    if date_heading:
        date_block = clean_text(date_heading.parent)
        time_match = re.search(r'\b([01]?\d|2[0-3])h([0-5]\d)\b', date_block)
        if time_match:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    locations = soup.select('main .location')
    venue = parse_venue(locations[0]) if locations else ''
    location_text = clean_text(locations[1]) if len(locations) > 1 else ''
    city = parse_city(location_text)
    country_code = country_for(location_text)

    description_parts = []
    for element in soup.select('main p'):
        text = clean_text(element)
        if len(text) >= 30 and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(item):
    url = item.get('link', '').strip()
    if not url:
        return None
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class LesPaladinsComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lespaladins_com',
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
        response = requests.get(
            API_URL,
            params={'per_page': 100, 'page': 1, '_fields': 'link'},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        items = response.json()
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        for page in range(2, total_pages + 1):
            page_response = requests.get(
                API_URL,
                params={'per_page': 100, 'page': page, '_fields': 'link'},
                headers=HEADERS,
                timeout=45,
            )
            page_response.raise_for_status()
            items.extend(page_response.json())

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, item): item for item in items}
            for future in as_completed(futures):
                url = futures[future].get('link', '')
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Les Paladins event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Les Paladins event',
                        event='crawler_item_skipped', level='warning', url=url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, venue, or city is missing',
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    LesPaladinsComCrawler().run()


if __name__ == '__main__':
    main()
