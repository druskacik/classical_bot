import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://imagosloveniae.net/'
SOURCE = 'Imago Sloveniae'
ARCHIVE_URL = urljoin(SOURCE_URL, 'arhiv-dogodkov/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sl-SI,sl;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'marec': 3,
    'april': 4,
    'maj': 5,
    'junij': 6,
    'julij': 7,
    'avgust': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'december': 12,
}

# The site stores the venue and municipality in one free-text field. These
# names cover recurring venues whose municipality is not written explicitly.
VENUE_CITIES = {
    'cankarjev dom': 'Ljubljana',
    'cerkev sv. jakoba': 'Ljubljana',
    'cerkev sv. jožefa': 'Ljubljana',
    'cerkev sv. trojice': 'Ljubljana',
    'cerkev sv. frančiška': 'Ljubljana',
    'festivalna dvorana': 'Ljubljana',
    'gornji trg': 'Ljubljana',
    'katedrala sv. nikolaja': 'Ljubljana',
    'križanke': 'Ljubljana',
    'ljubljanski grad': 'Ljubljana',
    'mestni muzej': 'Ljubljana',
    'narodna galerija': 'Ljubljana',
    'novi trg': 'Ljubljana',
    'stara mestna elektrarna': 'Ljubljana',
    'staro mestno jedro': 'Ljubljana',
    'zoo ljubljana': 'Ljubljana',
    'zrc sazu': 'Ljubljana',
}

FOREIGN_CITIES = {
    'bleiburg': ('Pliberk', 'AT'),
    'pliberk': ('Pliberk', 'AT'),
    'klagenfurt': ('Celovec', 'AT'),
    'celovec': ('Celovec', 'AT'),
    'gorizia': ('Gorica', 'IT'),
    'gorica': ('Gorica', 'IT'),
    'trieste': ('Trst', 'IT'),
    'trst': ('Trst', 'IT'),
    'szentgotthárd': ('Monošter', 'HU'),
    'szentgotthard': ('Monošter', 'HU'),
    'monošter': ('Monošter', 'HU'),
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\ufffd', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def archive_year(item):
    accordion = item.find_parent(class_='accordion')
    if accordion and accordion.get('id', '').startswith('accordion'):
        heading = accordion.find_previous('h3')
        match = re.search(r'\b(20\d{2})\b', clean_text(heading))
        if match:
            return int(match.group(1))
    return None


def parse_archive_item(item):
    link = item.select_one('a[href]')
    date_text = clean_text(item.select_one('.datum'))
    venue_text = clean_text(item.select_one('.prizorisce'))
    year = archive_year(item)
    match = re.search(r'\b(\d{1,2})\s*[./]\s*(\d{1,2})\b', date_text)
    if not link or not year or not match or not venue_text:
        return None
    try:
        event_date = date(year, int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None
    return {
        'date': event_date,
        'url': urljoin(SOURCE_URL, link['href']),
        'listing_venue': venue_text,
    }


def parse_location(value):
    def without_address(venue):
        parts = [part.strip() for part in re.split(r'\s+-\s+', venue) if part.strip()]
        while len(parts) > 1 and re.search(r'\d', parts[-1]):
            parts.pop()
        return ' - '.join(parts).strip()

    value = re.sub(r'\s+', ' ', value).strip(' ,;-')
    if not value:
        return None

    lower = value.casefold()
    if 'nova gorica' in lower:
        venue = without_address(re.split(r'\s+-\s+|,\s*', value)[0].strip())
        if venue.casefold() == 'nova gorica':
            return None
        return venue, 'Nova Gorica', 'SI'
    for token, (city, country_code) in FOREIGN_CITIES.items():
        if token in lower:
            venue = without_address(re.split(r'\s+-\s+|,\s*', value)[0].strip() or value)
            if venue.casefold() == city.casefold():
                return None
            return venue, city, country_code

    parts = [part.strip() for part in re.split(r'\s+-\s+', value) if part.strip()]
    if (
        len(parts) >= 2
        and len(parts[-1].split()) <= 4
        and not re.search(r'\d|\b(?:ulica|cesta|trg)\b', parts[-1], re.IGNORECASE)
    ):
        venue = without_address(' - '.join(parts[:-1]))
        return (venue, parts[-1], 'SI') if venue else None

    comma_parts = [part.strip() for part in value.split(',') if part.strip()]
    if (
        len(comma_parts) >= 2
        and len(comma_parts[-1].split()) <= 3
        and not re.search(r'\d|\b(?:ulica|cesta|trg)\b', comma_parts[-1], re.IGNORECASE)
    ):
        venue = without_address(', '.join(comma_parts[:-1]))
        return (venue, comma_parts[-1], 'SI') if venue else None

    if 'ljubljana' in lower:
        if lower == 'ljubljana':
            return None
        return value, 'Ljubljana', 'SI'
    for venue_token, city in VENUE_CITIES.items():
        if venue_token in lower:
            return value, city, 'SI'
    return None


def parse_detail(html, seed):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('#single-event')
    if event is None:
        return None

    title = clean_text(event.select_one('h1'))
    venue_text = clean_text(event.select_one('.single-prizorisce')) or seed['listing_venue']
    location = parse_location(venue_text)
    if not title or not location:
        return None

    time_text = clean_text(event.select_one('.single-ura'))
    time_match = re.search(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\b', time_text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    description = clean_text(event.select_one('.ref-vsebina')) or None
    venue, city, country_code = location
    return {
        'title': title,
        'date': seed['date'],
        'url': seed['url'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ImagoSloveniaeNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='imagosloveniae_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SI',
        upload_target='potential',
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
        dedupe_subset=['url', 'date'],
    )

    def _fetch_detail(self, seed):
        try:
            response = requests.get(seed['url'], headers=HEADERS, timeout=45)
            response.raise_for_status()
            return parse_detail(response.content, seed)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Imago Sloveniae event',
                event='crawler_detail_fetch_failed',
                level='warning',
                url=seed['url'],
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        try:
            response = requests.get(ARCHIVE_URL, headers=HEADERS, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Imago Sloveniae archive',
                event='crawler_fetch_failed',
                level='error',
                url=ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.content, 'html.parser')
        seeds_by_key = {}
        for item in soup.select('.accordion .eventi-list li'):
            seed = parse_archive_item(item)
            if seed:
                seeds_by_key[(seed['url'], seed['date'])] = seed

        records = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(self._fetch_detail, seed) for seed in seeds_by_key.values()]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ImagoSloveniaeNetCrawler().run()


if __name__ == '__main__':
    main()
