import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.stuttgarter-kammerorchester.com/index-en'
BASE_URL = 'https://www.stuttgarter-kammerorchester.com/'
SOURCE = 'Stuttgarter Kammerorchester'
LIST_URLS = (urljoin(BASE_URL, 'all-concerts'), urljoin(BASE_URL, 'archive'))
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
}

# Locations without the city after a comma occur regularly in the calendar.
# The list is intentionally limited to unambiguous venues published by SKO.
VENUE_CITIES = {
    'altes schloss stuttgart': 'Stuttgart', 'anna haag mehrgenerationenhaus': 'Stuttgart',
    'aula am berliner ring': 'Monheim', 'basf-feierabendhaus': 'Ludwigshafen',
    'backnanger bürgerhaus': 'Backnang', 'bürgerhaus backnang': 'Backnang',
    'carmen würth forum': 'Künzelsau', 'christuskirche stuttgart': 'Stuttgart',
    'concertgebouw': 'Amsterdam', 'elbphilharmonie': 'Hamburg',
    'filharmonia narodowa': 'Warschau', 'heinrich-lades-halle': 'Erlangen',
    'faustforum': 'Knittlingen', 'forum am schlosspark': 'Ludwigsburg',
    'forum am schlossplatz': 'Bietigheim-Bissingen', 'franziskaner konzerthaus': 'Villingen-Schwenningen',
    'gaisburger kirche': 'Stuttgart', 'grand théâtre': 'Luxembourg',
    'hospitalhof, paul-lechler-saal': 'Stuttgart', 'im wizemann': 'Stuttgart',
    'isarphilharmonie': 'München', 'johanneskirche stuttgart': 'Stuttgart',
    'kloster eberbach': 'Eltville am Rhein', 'konzerthaus': 'Berlin',
    'kultur- und kongresszentrum oberschwaben': 'Weingarten', 'kulturzentrum saalbau': 'Witten',
    'kulturzentrum schützi': 'Olten', 'kunst museum stuttgart': 'Stuttgart',
    'kunstmuseum stuttgart': 'Stuttgart', 'lindenhalle': 'Ehingen',
    'kölner philharmonie': 'Köln', 'markuskirche stuttgart': 'Stuttgart', 'musikverein': 'Wien',
    'ohrenberghalle bad schönborn': 'Bad Schönborn', 'ottakringer brauerei': 'Wien',
    'palatin - staufersaal': 'Wiesloch', 'phil harmonie essen': 'Essen',
    'philharmonie essen': 'Essen', 'raiffeisen-volksbank ries eg': 'Oettingen',
    'residenzschloss': 'Oettingen', 'rokoko-theater': 'Schwetzingen',
    'saal der bayerischen musikakademie': 'Marktoberdorf', 'saal des reitstadels': 'Neumarkt',
    'stadthalle gro ßer saal': 'Memmingen', 'stadthalle großer saal': 'Memmingen',
    'stuttgarter westen, open-air': 'Stuttgart',
    'stadthalle am schloss': 'Aschaffenburg', 'staatsoper prag': 'Prag',
    'stadtbibliothek stuttgart': 'Stuttgart', 'stiftskirche stuttgart': 'Stuttgart',
    'tagungszentrum onoldia': 'Ansbach', 'theater am ring': 'Saarlouis',
    'tonhalle zürich': 'Zürich', 'wilhelma theater stuttgart': 'Stuttgart',
    'wilhelmatheater stuttgart': 'Stuttgart', 'zehnthof zuffenhausen': 'Stuttgart',
}
COUNTRY_BY_CITY = {
    'Amsterdam': 'NL', 'Basel': 'CH', 'Beijing': 'CN', 'Bregenz': 'AT',
    'Brugg': 'CH', 'Bruneck': 'IT', 'Dornbirn': 'AT', 'Echternach': 'LU',
    'Erl': 'AT', 'Innsbruck': 'AT', 'Kraków': 'PL', 'Kufstein': 'AT',
    'Linz': 'AT', 'Ljubljana': 'SI', 'Luxembourg': 'LU', 'Lusławice': 'PL',
    'La Chaux de Fonds': 'CH', 'Maribor': 'SI', 'Muri': 'CH', 'Murten': 'CH',
    'Olten': 'CH', 'Prag': 'CZ', 'St. Vith': 'BE', 'Strasbourg': 'FR',
    'Warschau': 'PL', 'Wien': 'AT', 'Zürich': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=12, pool_maxsize=12,
        max_retries=Retry(total=3, backoff_factor=0.6, status_forcelist=(429, 500, 502, 503, 504)),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def city_from_location(location):
    folded = location.casefold()
    # Prefer explicit city names. Longest first avoids matching a district in a
    # longer city name and also covers the many "venue, city" variants.
    known = set(COUNTRY_BY_CITY) | set(VENUE_CITIES.values()) | {
        'Alpirsbach', 'Altensteig', 'Ansbach', 'Aschaffenburg', 'Backnang',
        'Bad Cannstatt', 'Bad Homburg', 'Bad Vilbel', 'Baden-Baden', 'Bayreuth', 'Bietigheim-Bissingen',
        'Blaibach', 'Bonn', 'Celle', 'Coesfeld', 'Dachau', 'Dresden', 'Ehingen',
        'Düsseldorf', 'Ellwangen', 'Eltville am Rhein', 'Fellbach', 'Friedrichshafen', 'Freiburg',
        'Gauting', 'Germering', 'Gersthofen', 'Göppingen', 'Hamburg', 'Hildesheim',
        'Kleve', 'Knittlingen', 'Koblenz', 'Kronberg', 'Künzelsau', 'Ladenburg',
        'Lahr', 'Landshut', 'Laupheim', 'Leipzig', 'Lörrach', 'Ludwigsburg',
        'Lüdenscheid', 'Maulbronn', 'Marktoberdorf', 'Mestlin', 'Monheim',
        'München', 'Neumarkt', 'Nürnberg', 'Ochsenhausen', 'Oettingen', 'Offenburg',
        'Erlangen', 'Fischbach', 'Köln', 'La Chaux de Fonds', 'Memmingen', 'Murten',
        'Passau', 'Pirmasens', 'Quakenbrück', 'Ravensburg', 'Rheinfelden', 'Rosenheim',
        'Rottenburg', 'Saarlouis', 'Schwäbisch Gmünd', 'Schwäbisch Hall', 'Stuttgart',
        'Starzach', 'Tauberbischofsheim', 'Trossingen', 'Tübingen', 'Tuttlingen', 'Viersen',
        'Villingen-Schwenningen', 'Waiblingen', 'Waldenbuch', 'Weikersheim',
        'Weil im Schönbuch', 'Weingarten', 'Wiesbaden', 'Wiesloch', 'Wismar',
        'Witten', 'Würzburg',
    }
    matches = [city for city in known if re.search(rf'(?<!\w){re.escape(city.casefold())}(?!\w)', folded)]
    if matches:
        return max(matches, key=len)
    return VENUE_CITIES.get(folded.strip(' ,'))


def parse_card(card, listing_url):
    title = clean_text(card.select_one('h2[itemprop="name"]'))
    moment = card.select_one('time[itemprop="startDate"][datetime]')
    location = clean_text(card.select_one('[itemprop="location"] [itemprop="name"]'))
    if not title or not moment or not location:
        return None
    try:
        event_date = date.fromisoformat(moment['datetime'][:10]).isoformat()
    except (KeyError, TypeError, ValueError):
        return None
    city = city_from_location(location) or city_from_location(title)
    if not city:
        return None
    link = card.select_one('h2 a[href]') or card.select_one('a[href]')
    url = urljoin(BASE_URL, link['href']) if link else listing_url
    time_match = re.search(r'T(\d{2}):(\d{2})', moment.get('datetime', ''))
    description = clean_text(card.select_one('[itemprop="description"]')) or None
    return {
        'title': title, 'date': event_date, 'url': url,
        'time_from': ':'.join(time_match.groups()) if time_match else None,
        'venue': location, 'city': city,
        'country_code': COUNTRY_BY_CITY.get(city, 'DE'), 'description': description,
        'source_url': SOURCE_URL, 'source': SOURCE,
    }


def enrich_record(session, record):
    parsed = urlparse(record['url'])
    if parsed.netloc != urlparse(BASE_URL).netloc or not parsed.path.startswith('/event-detail/'):
        return record
    soup = get_soup(session, record['url'])
    event = soup.select_one('.event.layout_full') or soup.select_one('[itemscope][itemtype="http://schema.org/Event"]')
    if not event:
        return record
    # The full event body retains programme and work names. Ticket links and
    # addresses are harmless here and are never reused as venue/city fields.
    description_parts = [
        clean_text(node) for node in event.select('.ce_text')
        if 'veranstalter' not in (node.get('class') or [])
        and 'ticket' not in (node.get('class') or [])
    ]
    description = '\n\n'.join(part for part in description_parts if part)
    if description:
        record['description'] = description
    return record


class StuttgarterKammerorchesterComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stuttgarter_kammerorchester_com', source=SOURCE, source_url=SOURCE_URL,
        country_code='DE', upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        records = []
        for listing_url in LIST_URLS:
            soup = get_soup(session, listing_url)
            for card in soup.select('.mod_eventlist .event[itemtype="http://schema.org/Event"]'):
                record = parse_card(card, listing_url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped SKO event without a valid date, venue, or city',
                        event='crawler_item_skipped', level='warning', url=listing_url,
                        error_type='IncompleteEventData',
                        error_message='Required event field could not be extracted',
                    )
        unique = {(r['title'], r['date'], r['time_from'], r['venue'], r['city']): r for r in records}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(enrich_record, session, item): item for item in unique.values()}
            enriched = []
            for future in as_completed(futures):
                record = futures[future]
                try:
                    enriched.append(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to enrich SKO event detail', event='crawler_item_failed',
                        level='warning', url=record['url'], error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    enriched.append(record)
        return sorted(enriched, key=lambda r: (r['date'], r['time_from'] or '', r['city'], r['title']))


def main():
    StuttgarterKammerorchesterComCrawler().run()


if __name__ == '__main__':
    main()
