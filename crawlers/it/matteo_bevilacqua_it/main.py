import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.matteo-bevilacqua.it/'
CALENDAR_URL = f'{SOURCE_URL}calendario/'
SOURCE = 'Matteo Bevilacqua'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# The calendar is an Italian artist's touring schedule.  Locations abroad must
# override the crawler's home-country code rather than turning it into a
# multi-country source.
CITY_COUNTRIES = {
    'Acquasparta': 'IT', 'Adria': 'IT', 'Andria': 'IT', 'Avezzano': 'IT',
    'Barcellona': 'ES', 'Brussels': 'BE', 'Bruxelles': 'BE', 'Buttrio': 'IT',
    'Campobasso': 'IT', 'Carmagnola': 'IT', 'Cividale': 'IT',
    'Colloredo di Monte Albano': 'IT', 'Cordenons': 'IT', 'Dubai': 'AE',
    'Feletto': 'IT', 'Finale Ligure': 'IT', 'Gemona': 'IT', 'Gorizia': 'IT',
    'Grado': 'IT', 'Jodoigne': 'BE', 'Knokke le Zoute': 'BE', 'La Spezia': 'IT',
    'Lerici': 'IT', 'Lignano': 'IT', 'Milano': 'IT', 'Modica': 'IT',
    'Monfalcone': 'IT', 'Mons': 'BE', 'Palmanova': 'IT', 'Pavia': 'IT',
    'Pescara': 'IT', 'Polcenigo': 'IT', 'Pontebba': 'IT', 'Porcia': 'IT',
    'Povoletto': 'IT', 'Radicondoli': 'IT', 'Ravenna': 'IT', 'Roma': 'IT',
    'Sacile': 'IT', 'Sarzana': 'IT', 'Tolmezzo': 'IT', 'Torino': 'IT',
    'Tortona': 'IT', 'Tricesimo': 'IT', 'Trieste': 'IT', 'Uccle': 'BE',
    'Udine': 'IT', 'Verona': 'IT', 'Vienna': 'AT', 'Waterloo': 'BE',
}

VENUE_CITY_DEFAULTS = {
    'Accademia di Danimarca': 'Roma',
    'Antwerp Spring Festival': 'Antwerp',
    'Auditorium di Povoletto': 'Povoletto',
    'Queen Elisabeth Music Chapel': 'Waterloo',
    'Teatro Civico La Spezia': 'La Spezia',
    'Teatro Comunale di Monfalcone': 'Monfalcone',
    'Teatro di Cordenons': 'Cordenons',
    'Teatro di Gemona': 'Gemona',
    'Teatro Luigi Bon': 'Tavagnacco',
    'Teatro Nuovo Giovanni da Udine': 'Udine',
}

EXTRA_COUNTRIES = {'Antwerp': 'BE', 'Tavagnacco': 'IT'}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})(?:\s+ore\s*(\d{1,2}):(\d{2}))?',
        clean_text(value), re.I,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%d %B %Y'
        ).date().isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        time_from = f'{int(match.group(4)):02d}:{match.group(5)}'
    return event_date, time_from


def resolve_location(value):
    location = clean_text(value)
    if not location or re.search(r'luogo da definire', location, re.I):
        return None, None, None

    city = None
    country_code = None
    candidates = {**CITY_COUNTRIES, **EXTRA_COUNTRIES}
    defaulted_venue = False
    for marker, default_city in VENUE_CITY_DEFAULTS.items():
        if marker.lower() in location.lower():
            city = default_city
            country_code = candidates[city]
            defaulted_venue = location.lower() == marker.lower()
            break
    for candidate in sorted(candidates, key=len, reverse=True):
        if re.search(rf'(?<!\w){re.escape(candidate)}(?!\w)', location, re.I):
            city = candidate
            country_code = candidates[candidate]
            break

    if not city:
        return None, None, None

    venue = location
    venue = re.sub(r'\s*\((?:IT|Italia|Italy|Belgium|BE|UD|RO)\)\s*', ' ', venue, flags=re.I)
    venue = re.sub(r'\b(?:Italy|Italia|Belgium)\b', ' ', venue, flags=re.I)
    if not defaulted_venue:
        # Remove a city only when it is a location segment.  City names can be
        # part of a legitimate venue or ensemble name (for example "Teatro
        # Nuovo Giovanni da Udine" and "Roma 3 Orchestra").
        venue = re.sub(rf'^\s*{re.escape(city)}\s*[,|–-]\s*', '', venue, flags=re.I)
        venue = re.sub(rf'\s*[,|–-]\s*{re.escape(city)}\s*$', '', venue, flags=re.I)
    venue = re.sub(r'^[\s,|–-]+|[\s,|–-]+$', '', venue)
    venue = re.sub(r'\s*[|,–-]\s*(?:\|\s*)?', ' | ', venue)
    venue = re.sub(r'\s{2,}', ' ', venue).strip(' |,-')
    if not venue or venue.lower() == city.lower():
        return None, None, None
    return venue, city, country_code


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    header = soup.select_one('.header-page')
    title = clean_text(header.select_one('h1')) if header else ''
    date_text = clean_text(header.select_one('.data')) if header else ''
    location_text = clean_text(header.select_one('.luogo')) if header else ''
    event_date, time_from = parse_datetime(date_text)
    venue, city, country_code = resolve_location(location_text)
    content = soup.select_one('.contenuto-page .contenuto')
    description = clean_text(content) or None
    if not title or not event_date or not venue or not city or not country_code:
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


def fetch_detail(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url)


class MatteoBevilacquaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='matteo_bevilacqua_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        urls = sorted({
            link['href']
            for card in soup.select('.concerto, .concerto-passato')
            if (link := card.select_one('a[href*="/concerto/"]')) and link.get('href')
        })

        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Matteo Bevilacqua concert detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Matteo Bevilacqua concert',
                        event='crawler_item_skipped', level='warning', url=url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, venue, city, or country could not be resolved',
                    )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    MatteoBevilacquaItCrawler().run()


if __name__ == '__main__':
    main()
