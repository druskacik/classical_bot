import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://francescopiemontesi.com/'
SOURCE = 'Francesco Piemontesi'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
}

# The pianist tours internationally. New entries name the city in their body;
# older entries often name only a well-known venue and put the city in the post
# title. These aliases deliberately cover only unambiguous evidence on the site.
CITY_COUNTRIES = {
    'Aix-en-Provence': 'FR', 'Alicante': 'ES', 'Ankara': 'TR',
    'Antwerp': 'BE', 'Ascona': 'CH', 'Barcelona': 'ES', 'Basel': 'CH',
    'Berlin': 'DE', 'Berne': 'CH', 'Bienne': 'CH', 'Bilbao': 'ES',
    'Boswil': 'CH', 'Bremen': 'DE', 'Budapest': 'HU', 'Chicago': 'US',
    'Cincinnati': 'US', 'Copenhagen': 'DK', 'Costa Mesa': 'US',
    'Dresden': 'DE', 'Duszniki-Zdrój': 'PL', 'Florence': 'IT',
    'Frankfurt': 'DE', 'Geneva': 'CH', 'Gstaad': 'CH', 'Hamburg': 'DE',
    'Helsinki': 'FI', 'Innsbruck': 'AT', 'Istanbul': 'TR',
    'Katowice': 'PL', 'Krün': 'DE', 'London': 'GB', 'Los Angeles': 'US',
    'Lucerne': 'CH', 'Lugano': 'CH', 'Luxembourg': 'LU', 'Lyon': 'FR',
    'Lübeck': 'DE', 'Madrid': 'ES', 'Martigny': 'CH', 'Milan': 'IT',
    'Milano': 'IT', 'Minneapolis': 'US', 'Monaco': 'MC', 'Montreal': 'CA',
    'Naples': 'IT', 'New York': 'US', 'Oslo': 'NO', 'Palm Desert': 'US',
    'Paris': 'FR', 'Prague': 'CZ', 'Salzburg': 'AT', 'Santa Barbara': 'US',
    'Schwarzenberg': 'AT', 'Stavanger': 'NO', 'Stockholm': 'SE',
    'Strasbourg': 'FR', 'Tokyo': 'JP', 'Trieste': 'IT', 'Varna': 'BG',
    'Vienna': 'AT', 'Warsaw': 'PL', 'Washington DC': 'US', 'Zurich': 'CH',
}
COUNTRY_NAMES = {
    'Austria': 'AT', 'Canada': 'CA', 'Finland': 'FI', 'France': 'FR',
    'Germany': 'DE', 'Greece': 'GR', 'Hungary': 'HU', 'Iceland': 'IS',
    'Isreal': 'IL', 'Italy': 'IT', 'Korea': 'KR', 'Netherlands': 'NL',
    'Norway': 'NO', 'Poland': 'PL', 'Schweiz': 'CH', 'Slovakia': 'SK',
    'Spain': 'ES', 'Sweden': 'SE', 'Switzerland': 'CH',
    'Switzlerand': 'CH', 'Turkey': 'TR', 'UK': 'GB', 'USA': 'US',
}
TITLE_CITY_PATTERNS = (
    (r'^aix', 'Aix-en-Provence'), (r'^alicante', 'Alicante'),
    (r'^ankara', 'Ankara'), (r'^barcelona', 'Barcelona'),
    (r'^bfo', 'Budapest'), (r'^bilbao', 'Bilbao'), (r'^boswil', 'Boswil'),
    (r'^bremen', 'Bremen'), (r'^cinci', 'Cincinnati'),
    (r'^cso', 'Chicago'), (r'^dresden rezital', 'Dresden'),
    (r'^helsinki', 'Helsinki'), (r'^hr', 'Frankfurt'),
    (r'^innsbruck', 'Innsbruck'), (r'^istanbul', 'Istanbul'),
    (r'^kopenhagen', 'Copenhagen'), (r'^lille', 'Lille'),
    (r'^lucerne|^luzern', 'Lucerne'), (r'^martigny', 'Martigny'),
    (r'^milano', 'Milan'), (r'^minneapolis', 'Minneapolis'),
    (r'^monace', 'Monaco'), (r'^montreal', 'Montreal'),
    (r'^napoli', 'Naples'), (r'^new york', 'New York'),
    (r'^nhk', 'Tokyo'), (r'^onesp', 'Madrid'), (r'^paris|^onf$', 'Paris'),
    (r'^prag', 'Prague'), (r'^salzburg', 'Salzburg'),
    (r'^rai', 'Turin'), (r'^smascona|^settimane', 'Ascona'),
    (r'^schubertiade', 'Schwarzenberg'), (r'^schloss elmau', 'Krün'),
    (r'^gstaad', 'Gstaad'), (r'^duszniki', 'Duszniki-Zdrój'),
    (r'^bbc proms', 'London'), (r'^prinzregenten', 'Munich'),
    (r'^vevey', 'Vevey'), (r'^valladolid', 'Valladolid'),
    (r'^stavanger', 'Stavanger'), (r'^stockholm', 'Stockholm'),
    (r'^vienna|^wien', 'Vienna'), (r'^z.rich', 'Zurich'),
)
CITY_COUNTRIES['Lille'] = 'FR'
CITY_COUNTRIES.update({
    'Munich': 'DE', 'Turin': 'IT', 'Valladolid': 'ES', 'Vevey': 'CH',
})
VENUE_CITIES = {
    'Auditorio Nacional': 'Madrid',
    'Auditorio Nacional de Música': 'Madrid',
    'Chiesa di San Francesco': 'Locarno',
    'Kulturpalast': 'Dresden',
    'Kurhaus': 'Baden-Baden',
    'Maison de la Radio, Auditorium': 'Paris',
    'Palacio de Festivales': 'Santander',
    'Sala Verdi': 'Milan',
    'Teatro Filarmonico': 'Verona',
    'Teatro Galli': 'Rimini',
    'Théâtre Impérial': 'Compiègne',
}
CITY_COUNTRIES.update({
    'Baden-Baden': 'DE', 'Compiègne': 'FR', 'Locarno': 'CH',
    'Rimini': 'IT', 'Santander': 'ES', 'Verona': 'IT',
})


def clean_text(value):
    if not value:
        return ''
    raw = html.unescape(str(value))
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json(), response.headers


def list_events(session):
    events = []
    page = 1
    while True:
        payload, headers = get_json(
            session,
            EVENTS_API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
                '_fields': 'id,slug,link,title,content',
            },
        )
        events.extend(payload)
        total_pages = int(headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return events
        page += 1


def resolve_city(first_line, title):
    first_line = clean_text(first_line).rstrip(',')
    match = re.fullmatch(r'(.+?)\s*\(([^()]+)\)', first_line)
    if match:
        city = clean_text(match.group(1))
        country_code = COUNTRY_NAMES.get(clean_text(match.group(2)))
        if city and country_code:
            return city, country_code, True

    if first_line in CITY_COUNTRIES:
        return first_line, CITY_COUNTRIES[first_line], True

    if first_line in VENUE_CITIES:
        city = VENUE_CITIES[first_line]
        return city, CITY_COUNTRIES[city], False

    title = clean_text(title)
    for pattern, city in TITLE_CITY_PATTERNS:
        if re.search(pattern, title, re.I):
            return city, CITY_COUNTRIES[city], False
    return '', '', False


def parse_date_and_time(soup):
    main = soup.select_one('main') or soup
    text = main.get_text(' ', strip=True)
    match = re.search(
        r'Date:\s*(\d{1,2}\.\s+[A-Za-z]+\s+\d{4})', text, re.I
    )
    if not match:
        return '', None
    try:
        event_date = datetime.strptime(match.group(1), '%d. %B %Y').date().isoformat()
    except ValueError:
        return '', None

    time_match = re.search(r'Time:\s*(\d{1,2}:\d{2})(?:\s*-|\b)', text, re.I)
    time_from = time_match.group(1) if time_match else None
    # VSEL displays midnight-to-midnight for events whose editor supplied no time.
    if time_from == '0:00':
        time_from = None
    elif time_from and len(time_from) == 4:
        time_from = f'0{time_from}'
    return event_date, time_from


def choose_venue(lines, first_is_city):
    candidates = lines[1:] if first_is_city else lines
    if not candidates:
        return ''
    venue = candidates[0]
    if (
        venue not in VENUE_CITIES
        and len(candidates) > 1
        and re.search(r'festival|settimane|schubertiade', venue, re.I)
    ):
        # Some archive entries put a series/festival name before the actual hall.
        # Do not mistake Vevey's parenthesized date range for a venue, though.
        if re.fullmatch(r'\([^)]*\)', candidates[1]):
            return venue
        venue = candidates[1]
    if venue.casefold() in {'location tbd', 'tbd'}:
        return ''
    return venue


def parse_event(session, event):
    url = clean_text(event.get('link'))
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    event_date, time_from = parse_date_and_time(soup)

    content = (event.get('content') or {}).get('rendered') or ''
    content_soup = BeautifulSoup(content, 'html.parser')
    lines = [clean_text(node.get_text(' ', strip=True)) for node in content_soup.select('p')]
    lines = [line for line in lines if line]
    title_value = (event.get('title') or {}).get('rendered') or ''
    city, country_code, first_is_city = resolve_city(lines[0] if lines else '', title_value)
    venue = choose_venue(lines, first_is_city)
    description = '\n'.join(lines) or None

    # The WordPress title is often just an internal abbreviation ("NY", "BFO2").
    # The body is the public billing, so construct a useful title from its programme.
    title = clean_text(title_value)
    if lines:
        programme = lines[-1]
        if len(programme) >= 4 and programme.casefold() not in {city.casefold(), venue.casefold()}:
            title = programme

    if not all((title, event_date, url, venue, city, country_code)):
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


class FrancescoPiemontesiComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='francescopiemontesi_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = list_events(session)
        records = []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(parse_event, session, event): event for event in events}
            for future in as_completed(futures):
                event = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Francesco Piemontesi event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=clean_text(event.get('link')),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Francesco Piemontesi event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(event.get('link')),
                        error_type='IncompleteEventData',
                        error_message='Required date, title, venue, city, or country is missing',
                    )

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    FrancescoPiemontesiComCrawler().run()


if __name__ == '__main__':
    main()
