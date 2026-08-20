import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://anjabihlmaier.de/de/kalender/'
SOURCE = 'Anja Bihlmaier'
API_URL = 'https://anjabihlmaier.de/wp-json/wp/v2/kalenderitem'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

COUNTRY_NAMES = {
    'Australia': 'AU', 'Austria': 'AT', 'Belgium': 'BE', 'Canada': 'CA',
    'Denmark': 'DK', 'Deutschland': 'DE', 'England': 'GB', 'Finland': 'FI',
    'Finnland': 'FI', 'France': 'FR', 'Frankreich': 'FR', 'Germany': 'DE',
    'Hong Kong': 'HK', 'Ireland': 'IE', 'Japan': 'JP', 'Netherlands': 'NL',
    'Norway': 'NO', 'Schweden': 'SE', 'Spain': 'ES', 'Sweden': 'SE',
    'Switzerland': 'CH', 'UK': 'GB', 'USA': 'US', 'Österreich': 'AT',
}

# The calendar often publishes only the hall, not its city. These are stable,
# well-known venue locations represented in the source's current and archive feeds.
VENUE_LOCATIONS = {
    'Alte Oper Frankfurt': ('Frankfurt', 'DE'),
    'Auditorio de Tenerife': ('Santa Cruz de Tenerife', 'ES'),
    'Auditorio Nacional de Música': ('Madrid', 'ES'),
    'Barbican': ('London', 'GB'),
    'Beethovenhalle Bonn': ('Bonn', 'DE'),
    'Berwaldhallen': ('Stockholm', 'SE'),
    'Bozar': ('Brussels', 'BE'),
    'Bridgewater Hall': ('Manchester', 'GB'),
    'Brucknerhaus Linz': ('Linz', 'AT'),
    'Concertgebouw Brugge': ('Bruges', 'BE'),
    'Concertgebouw': ('Amsterdam', 'NL'),
    'Die Glocke': ('Bremen', 'DE'),
    'Elbphilharmonie': ('Hamburg', 'DE'),
    'Festspielhaus Erl': ('Erl', 'AT'),
    'Hamer Hall': ('Melbourne', 'AU'),
    'Helsinki Music Centre': ('Helsinki', 'FI'),
    'Hollywood Bowl': ('Los Angeles', 'US'),
    'Isarphilharmonie': ('Munich', 'DE'),
    'Konzerthaus Berlin': ('Berlin', 'DE'),
    'Konzerthaus Dortmund': ('Dortmund', 'DE'),
    'Kölner Philharmonie': ('Cologne', 'DE'),
    'L’Auditori Barcelona': ('Barcelona', 'ES'),
    'Musikverein Wien': ('Vienna', 'AT'),
    'National Concert Hall': ('Dublin', 'IE'),
    'Orchestra Hall': ('Minneapolis', 'US'),
    'Palau de les Arts': ('Valencia', 'ES'),
    'Philharmonie Berlin': ('Berlin', 'DE'),
    'Powell Hall': ('St. Louis', 'US'),
    'Queen Elizabeth Hall': ('London', 'GB'),
    'Royal Albert Hall': ('London', 'GB'),
    'Sibeliustalo': ('Lahti', 'FI'),
    'Sydney Opera House': ('Sydney', 'AU'),
    'Suntory Hall': ('Tokyo', 'JP'),
    'Teatre Calderón': ('Alcoi', 'ES'),
    'The Helsinki Music Centre': ('Helsinki', 'FI'),
    'Tokyo Metropolitan Theatre': ('Tokyo', 'JP'),
    'Tivoli Vredenburg': ('Utrecht', 'NL'),
    'Winthrop Hall': ('Perth', 'AU'),
}

CITY_COUNTRIES = {
    'Aachen': 'DE', 'Aabenraa': 'DK', 'Alcoi': 'ES', 'Amsterdam': 'NL',
    'Barcelona': 'ES', 'Berlin': 'DE', 'Bonn': 'DE', 'Brugge': 'BE',
    'Brussel': 'BE', 'Cologne': 'DE', 'Dortmund': 'DE', 'Dublin': 'IE',
    'Eindhoven': 'NL', 'Frankfurt': 'DE', 'Geelong': 'AU', 'Göteborg': 'SE',
    'Hamburg': 'DE', 'Hannover': 'DE', 'Helsinki': 'FI', 'Hongkong': 'HK',
    'Kassel': 'DE', 'Lahti': 'FI', 'Leipzig': 'DE', 'Linz': 'AT',
    "La Vall d'Uixo": 'ES',
    'London': 'GB', 'Lübeck': 'DE', 'Lyon': 'FR', 'Madrid': 'ES',
    'Manchester': 'GB', 'Mannheim': 'DE', 'Melbourne': 'AU', 'Minneapolis': 'US',
    'München': 'DE', 'Paris': 'FR', 'Perth': 'AU', 'Saarbrücken': 'DE',
    'Salford': 'GB', 'St. Louis': 'US', 'Stockholm': 'SE', 'Sydney': 'AU',
    'Heerlen': 'NL', 'Herleen': 'NL', 'Tokyo': 'JP', 'Turku': 'FI',
    'Utrecht': 'NL', 'Valencia': 'ES',
    'Wien': 'AT', 'Zürich': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_dates(value):
    value = clean_text(value)
    year_match = re.search(r'\b(20\d{2})\b', value)
    if not year_match:
        return []
    year = year_match.group(1)
    dates = []
    for month, day in re.findall(r'([A-Z][a-z]{2})\s+(\d{1,2})', value):
        try:
            dates.append(datetime.strptime(f'{month} {day} {year}', '%b %d %Y').date().isoformat())
        except ValueError:
            continue
    return dates


def infer_location(value):
    location = clean_text(value).strip(' ,')
    if not location:
        return None

    explicit_country = None
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf'\b{re.escape(name)}\b', location, re.I):
            explicit_country = code
            location = re.sub(rf'\s*,?\s*\b{re.escape(name)}\b\s*$', '', location, flags=re.I)
            break

    for venue_name, (city, code) in VENUE_LOCATIONS.items():
        if venue_name.casefold() in location.casefold():
            venue = re.sub(rf'\s*,\s*{re.escape(city)}\s*$', '', location, flags=re.I)
            return venue.strip(' ,'), city, explicit_country or code

    parts = [part.strip() for part in location.split(',') if part.strip()]
    for part in reversed(parts):
        if part in CITY_COUNTRIES:
            venue = ', '.join(parts[:-1]).strip() if part == parts[-1] else location
            if venue and venue.casefold() != part.casefold():
                return venue, part, explicit_country or CITY_COUNTRIES[part]

    # Some entries contain a known city as the final word without a comma.
    for city, code in sorted(CITY_COUNTRIES.items(), key=lambda item: -len(item[0])):
        match = re.search(rf'(?:,|\s)\s*{re.escape(city)}\s*$', location, re.I)
        if match:
            venue = location[:match.start()].strip(' ,-')
            if venue:
                return venue, city, explicit_country or code
    return None


def detail_descriptions():
    descriptions = {}
    page = 1
    while True:
        response = requests.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'link,content',
            },
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        items = response.json()
        for item in items:
            content = clean_text(BeautifulSoup(item.get('content', {}).get('rendered', ''), 'html.parser'))
            if content:
                descriptions[item['link'].rstrip('/')] = content
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return descriptions


def parse_calendar(html, descriptions):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    seen = set()
    for block in soup.select('div.newsblok'):
        date_element = block.find(['h3', 'h4'])
        title_element = block.find('h4') if date_element and date_element.name == 'h3' else block.find('h2')
        link = block.find('a', href=re.compile(r'/kalenderitem/'))
        location_element = block.find('p', style=re.compile(r'font-size\s*:\s*16px', re.I))
        if not date_element or not title_element or not link or not location_element:
            continue
        dates = parse_dates(date_element)
        location = infer_location(location_element)
        title = clean_text(title_element)
        url = link['href'].strip().rstrip('/')
        if not dates or not location or not title or url in seen:
            continue
        seen.add(url)
        venue, city, country_code = location
        summary_parts = [clean_text(element) for element in block.find_all(['h5', 'h6'])]
        description_parts = [part for part in summary_parts if part]
        detail = descriptions.get(url)
        if detail and detail not in description_parts:
            description_parts.append(detail)
        description = '\n\n'.join(description_parts) or None
        time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', detail or '')
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
        for event_date in dates:
            records.append({
                'title': title,
                'date': event_date,
                'url': url + '/',
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class AnjabihlmaierDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='anjabihlmaier_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        descriptions = detail_descriptions()
        records = parse_calendar(response.text, descriptions)
        if not records:
            log_message(
                'Anja Bihlmaier calendar returned no complete concerts',
                event='crawler_empty',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    AnjabihlmaierDeCrawler().run()


if __name__ == '__main__':
    main()
