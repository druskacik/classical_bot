import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.simon-hoefele.de/'
SOURCE = 'Simon Höfele'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar-input')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}
SITE_TIMEZONE = ZoneInfo('Europe/Berlin')

# Squarespace does not populate addressCountry for this calendar. These are the
# touring cities outside Germany found in the collection; German cities safely
# fall back to the artist/site's home country.
CITY_COUNTRIES = {
    'amsterdam': 'NL',
    'basel': 'CH',
    'bern': 'CH',
    'birmingham': 'GB',
    'brüssel': 'BE',
    'budapest': 'HU',
    'eindhoven': 'NL',
    'epinal': 'FR',
    'grafenegg': 'AT',
    'innsbruck': 'AT',
    'inssbruck': 'AT',
    'liechtenstein': 'LI',
    'lissabon': 'PT',
    'london': 'GB',
    'luzern': 'CH',
    'luxemburg': 'LU',
    'maastricht': 'NL',
    'newcastle': 'GB',
    'ostrava': 'CZ',
    'porto': 'PT',
    'stockholm': 'SE',
    'wien': 'AT',
    'winnipeg': 'CA',
    '‘s-hertogenbosch': 'NL',
    "'s-hertogenbosch": 'NL',
}
COUNTRY_NAMES = {
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'czech republic': 'CZ',
    'czechia': 'CZ',
    'france': 'FR',
    'germany': 'DE',
    'hungary': 'HU',
    'liechtenstein': 'LI',
    'luxembourg': 'LU',
    'netherlands': 'NL',
    'portugal': 'PT',
    'sweden': 'SE',
    'switzerland': 'CH',
    'united kingdom': 'GB',
}
VENUE_WORDS = re.compile(
    r'\b(?:aula|casino|cathedral|centennial|church|college|concert|congress|'
    r'conservatoire|erholungshaus|festival|gymnasium|hall|hansesaal|haus|hotel|'
    r'kammer|kirche|konserthus|konzerthaus|landtag|martinskirche|müpa|museum|'
    r'nikolaisaal|opera|orchestra|parade|philharmonie|reichssaal|saal|stadthalle|'
    r'staatstheater|studio|theater|theatre|universität|vrijthof|volkshaus)\b',
    re.IGNORECASE,
)


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def split_location(value):
    value = html.unescape(value or '').strip(' ,/|')
    value = re.sub(r'\s+', ' ', value)
    if not value:
        return None

    parts = re.split(r'\s*(?:,|/|\|)\s*', value, maxsplit=1)
    if len(parts) != 2 or not all(parts):
        return None
    city, venue = parts
    city = re.sub(r'\s*\(([A-Z]{2})\)\s*$', '', city).strip()
    if not city or not venue or city.casefold() == venue.casefold():
        return None
    return city, venue


def parse_location(item, description):
    location = item.get('location') or {}
    candidates = [location.get('addressLine2'), location.get('addressLine1')]
    description_locations = []
    for line in reversed(description.splitlines()):
        parsed_line = split_location(line)
        if parsed_line and VENUE_WORDS.search(parsed_line[1]):
            description_locations.append(line)
    candidates.extend(description_locations)
    candidates.append(html.unescape(item.get('title') or ''))

    parsed = None
    for value in candidates:
        parsed = split_location(value)
        if parsed:
            break
    if parsed is None:
        return None

    city, venue = parsed
    if city.casefold() == 'inssbruck':
        city = 'Innsbruck'
    raw_country = (location.get('addressCountry') or '').strip()
    if re.fullmatch(r'[A-Za-z]{2}', raw_country):
        country_code = raw_country.upper()
    else:
        country_code = COUNTRY_NAMES.get(raw_country.casefold())
    if not country_code:
        country_code = CITY_COUNTRIES.get(city.casefold(), 'DE')
    return city, venue, country_code


def parse_item(item):
    title = html.unescape(item.get('title') or '').strip()
    full_url = item.get('fullUrl') or ''
    start_timestamp = item.get('startDate')
    if not title or not full_url or not isinstance(start_timestamp, (int, float)):
        return None

    try:
        starts_at = datetime.fromtimestamp(start_timestamp / 1000, tz=SITE_TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return None

    body = clean_html(item.get('body'))
    excerpt = clean_html(item.get('excerpt'))
    description = '\n\n'.join(part for part in (body, excerpt) if part)
    location = parse_location(item, description)
    if location is None:
        return None
    city, venue, country_code = location

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SimonHoefeleDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='simon_hoefele_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        page_url = f'{CALENDAR_URL}?format=json'
        seen_pages = set()
        items = []

        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            try:
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Simon Höfele calendar',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            items.extend(payload.get('upcoming') or [])
            items.extend(payload.get('past') or [])
            next_path = (payload.get('pagination') or {}).get('nextPageUrl')
            if next_path:
                separator = '&' if '?' in next_path else '?'
                page_url = urljoin(SOURCE_URL, next_path) + f'{separator}format=json'
            else:
                page_url = None

        records = [record for item in items if (record := parse_item(item))]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SimonHoefeleDeCrawler().run()


if __name__ == '__main__':
    main()
