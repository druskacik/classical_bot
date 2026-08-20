import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.christinaconcours.nl/'
EVENTS_URL = urljoin(SOURCE_URL, 'evenementen')
SOURCE = 'Prinses Christina Concours'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'feb': 2, 'mrt': 3, 'apr': 4, 'mei': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}

# The calendar often prints a street address instead of a venue name. These
# first-party address/name pairs are stable enough to avoid using an address as
# the venue while retaining otherwise valid events.
VENUES = {
    'baden powelllaan 2': 'Museumpark',
    'spui 175': 'Nieuwe Kerk',
    'gasthuisstraat 41': 'Podium Doesburg',
    'concertgebouwplein 10': 'Concertgebouw Amsterdam',
    'keizer karelplein 2d': 'De Vereeniging',
    'kerkplein 1': 'Agathakerk',
    'piet heinkade 1': "Muziekgebouw aan 't IJ",
    'heuvel 140': 'Muziekgebouw Eindhoven',
    'kerkstraat 35': 'Dorpskerk Wilp',
    'kloosterstraat 1': 'Concertzaal Tilburg',
    'boschdijkstraat 45': 'Verkadefabriek',
    'vredenburgkade 11': 'TivoliVredenburg',
    'geertekerkhof 23': 'Geertekerk',
    'stationsplein 1': 'Theater De Meenthe',
    'weimarstraat 63': 'Theater De Regentes',
    'coehoornsingel 1': 'Theater Hanzehof',
    'velperbinnensingel 15': 'Musis & Stadstheater Arnhem',
    'lange begijnestraat 11': 'Philharmonie Haarlem',
    'schouwburgplein 50': 'de Doelen',
    'parade 23': 'Theater aan de Parade',
    'breestraat 60': 'Stadsgehoorzaal Leiden',
    'spuiplein 150': 'Amare',
    'westzijde 148': 'Muziekschool FluXus',
    'meeuwerderweg 1': 'De Oosterpoort',
    'verlengde noordkade 10-12': 'CHV Noordkade',
    'utrechtsestraat 85': 'ArtEZ Conservatorium Arnhem',
    'haagseveer 4': 'Codarts Rotterdam',
    'amstelveld 10': 'Amstelkerk',
    'vondelpark 5a': 'Vondelpark Openluchttheater',
    'keukenhof 1': 'Kasteel Keukenhof',
    'victor westhofflaan 22': 'Botanische Tuin de Hortus Nijmegen',
}

CITY_ALIASES = {
    "'s-hertogenbosch": "'s-Hertogenbosch",
    'den bosch': "'s-Hertogenbosch",
    'den haag': 'Den Haag',
    'monchengladbach': 'Mönchengladbach',
    'mönchengladbach': 'Mönchengladbach',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_time(value):
    match = re.search(r'(?<!\d)(\d{1,2})[.:](\d{2})(?!\d)', value or '')
    if not match:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def country_for(location):
    value = location.casefold()
    if 'belgië' in value or re.search(r'\b(?:antwerpen|turnhout|hoogstraten)\b', value):
        return 'BE'
    if 'mönchengladbach' in value or 'schloss rheydt' in value:
        return 'DE'
    return 'NL'


def city_for(location):
    value = clean_text(location)
    folded = value.casefold()
    for candidate in (
        'Mönchengladbach', 'Rotterdam', 'Utrecht', 'Amsterdam', 'Gorinchem',
        'Voorburg', 'Doesburg', 'Antwerpen', 'Nijmegen', 'Breda', 'Zandvoort',
        'Bovenkarspel', 'Eindhoven', 'Wilp', 'Tilburg', 'Turnhout',
        "'s-Hertogenbosch", 'Den Bosch', 'Steenwijk', 'Rhenen', 'Den Haag',
        'Zutphen', 'Hoogstraten', 'Arnhem', 'Zaandam', 'Haarlem', 'Groningen',
        'Veghel', 'Leiden', 'Oranjewoud', 'Amersfoort', 'Zwolle', 'Warmond',
        'Lisse', 'Bergharen', 'Hoeven',
    ):
        if re.search(rf'(?<!\w){re.escape(candidate.casefold())}(?!\w)', folded):
            return CITY_ALIASES.get(candidate.casefold(), candidate)
    return None


def venue_for(location, title):
    value = clean_text(location)
    folded = value.casefold()
    if not value or folded in {'online', 'n.t.b.', 'ntb'}:
        return None
    for address, venue in VENUES.items():
        if address in folded:
            return venue

    # A leading phrase before the comma is a venue only if it is not merely a
    # city or street address.
    first = value.split(',', 1)[0].strip()
    venue_word = re.search(
        r'\b(?:chassé|concertgebouw|cultuurhuis|gc |kerk|museum|podium|schloss|'
        r'theater|verkadefabriek)\b', first, re.I,
    )
    if (
        ',' in value
        and (venue_word or not city_for(first))
        and not re.search(r'\b\d+[a-z]?\b', first, re.I)
        and first.casefold() not in {'n.t.b.', 'online'}
    ):
        return first

    title_folded = title.casefold()
    inferred = {
        'klassiek op het amstelveld': 'Amstelveld',
        'vondelpark openluchttheater': 'Vondelpark Openluchttheater',
        'gaudeamus festival': 'TivoliVredenburg',
        'hét postkantoor': 'Hét Postkantoor',
        'kasteel keukenhof': 'Kasteel Keukenhof',
        'botanische tuin de hortus': 'Botanische Tuin de Hortus Nijmegen',
    }
    for phrase, venue in inferred.items():
        if phrase in title_folded:
            return venue
    return None


def event_groups(soup):
    current_year = date.today().year
    for heading in soup.select('main h2'):
        text = clean_text(heading)
        year_match = re.search(r'\b(20\d{2})\b', text)
        year = int(year_match.group(1)) if year_match else current_year
        listing = heading.find_next_sibling('ul')
        if listing:
            yield year, listing.find_all('li', recursive=False)


def parse_card(card, year):
    title_node = card.find('h3')
    link = title_node.find_parent('a', href=True) if title_node else None
    spans = card.find_all('span')
    if not title_node or not link or len(spans) < 3:
        return None
    title = clean_text(title_node)
    day_text, month_text, time_text = (clean_text(node) for node in spans[:3])
    month = MONTHS.get(month_text[:3].casefold())
    try:
        event_date = date(year, month, int(day_text)).isoformat()
    except (TypeError, ValueError):
        return None

    location_node = card.select_one('div a[href*="maps"], div a[href*="goo.gl"]')
    location = clean_text(location_node)
    city = city_for(location) or city_for(title)
    venue = venue_for(location, title)
    if not title or not city or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, link.get('href')),
        'time_from': parse_time(time_text),
        'venue': venue,
        'city': city,
        'country_code': country_for(location),
        'description': None,
    }


def get_concerts():
    response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    skipped_count = 0
    for year, cards in event_groups(soup):
        for card in cards:
            record = parse_card(card, year)
            if record:
                records.append(record)
            else:
                skipped_count += 1
    log_message(
        'Calendar parsed',
        event='crawler_calendar_parsed',
        url=EVENTS_URL,
        record_count=len(records),
        skipped_count=skipped_count,
    )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ChristinaConcoursNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='christinaconcours_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ChristinaConcoursNlCrawler().run()


if __name__ == '__main__':
    main()
