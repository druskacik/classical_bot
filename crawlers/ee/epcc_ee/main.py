import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.epcc.ee/'
CONCERTS_URL = urljoin(SOURCE_URL, 'kontserdid/')
SOURCE = 'Eesti Filharmoonia Kammerkoor'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'et-EE,et;q=0.9,en;q=0.7',
}

# Names used by the Estonian-language calendar.  A location without a
# defensible city is deliberately skipped rather than filled with the choir's
# home city: EPCC tours extensively.
COUNTRIES = {
    'argentiina': 'AR', 'austraalia': 'AU', 'austria': 'AT', 'belgia': 'BE',
    'brasiilia': 'BR', 'eesti': 'EE', 'hiina': 'CN', 'hispaania': 'ES',
    'holland': 'NL', 'horvaatia': 'HR', 'iirimaa': 'IE', 'israel': 'IL',
    'itaalia': 'IT', 'jaapan': 'JP', 'kanada': 'CA', 'kreeka': 'GR',
    'lõuna-korea': 'KR', 'läti': 'LV', 'leedu': 'LT', 'luksemburg': 'LU',
    'malta': 'MT', 'mehhiko': 'MX', 'norra': 'NO', 'poola': 'PL',
    'portugal': 'PT', 'prantsusmaa': 'FR', 'põhja-iirimaa': 'GB',
    'rootsi': 'SE', 'rumeenia': 'RO', 'saksamaa': 'DE', 'singapur': 'SG',
    'slovakkia': 'SK', 'sloveenia': 'SI', 'soome': 'FI', 'suurbritannia': 'GB',
    'suurbritannnia': 'GB', 'šotimaa': 'GB', 'shotimaa': 'GB', 'šveits': 'CH',
    'taani': 'DK', 'tšehhi': 'CZ', 'türgi': 'TR', 'uk': 'GB', 'ungari': 'HU',
    'usa': 'US',
}

# Ordered longest/most-specific first.  Aliases include Estonian inflections
# and well-known halls whose listing omits the city.
CITY_ALIASES = {
    'aix-en-provence': 'Aix-en-Provence', 'bergisch-gladbach': 'Bergisch Gladbach',
    'clermont': 'Clermont-Ferrand', 'derry/londonderry': 'Derry',
    'hertogenbosch': "'s-Hertogenbosch", 'la roque-d’anthéron': 'La Roque-d’Anthéron',
    "la roque-d'enthéron": 'La Roque-d’Anthéron', 'põhja-carolina': 'Durham',
    'saint-galmier': 'Saint-Galmier', 'schwaebisch gmuend': 'Schwäbisch Gmünd',
    'west lafayette': 'West Lafayette', 'a coruña': 'A Coruña',
    'aarhus': 'Aarhus', 'aldeburgh': 'Aldeburgh', 'alūksne': 'Alūksne',
    'amsterdam': 'Amsterdam', 'ankara': 'Ankara', 'ann arbor': 'Ann Arbor',
    'antwerpen': 'Antwerp', 'arnhem': 'Arnhem', 'assisi': 'Assisi',
    'barcelona': 'Barcelona', 'basel': 'Basel', 'beijing': 'Beijing',
    'peking': 'Beijing', 'bergen': 'Bergen', 'bergamo': 'Bergamo',
    'berliin': 'Berlin', 'berlin': 'Berlin', 'bielefeld': 'Bielefeld',
    'bologna': 'Bologna', 'bolzano': 'Bolzano', 'bremen': 'Bremen',
    'brescia': 'Brescia', 'bressanone': 'Bressanone', 'brügge': 'Bruges',
    'brugge': 'Bruges', 'brüssel': 'Brussels', 'brunico': 'Brunico',
    'budapest': 'Budapest', 'buenos aires': 'Buenos Aires', 'bydgoszcz': 'Bydgoszcz',
    'cambridge': 'Cambridge', 'canberra': 'Canberra', 'cardiff': 'Cardiff',
    'charleroi': 'Charleroi', 'changsha': 'Changsha', 'cincinnati': 'Cincinnati',
    'cork': 'Cork', 'dortmund': 'Dortmund', 'dublin': 'Dublin',
    'dublini': 'Dublin', 'durham': 'Durham', 'dresden': 'Dresden',
    'edinburgh': 'Edinburgh', 'eindhoven': 'Eindhoven', 'eisenach': 'Eisenach',
    'essen': 'Essen', 'espoo': 'Espoo', 'ferrara': 'Ferrara', 'firenze': 'Florence',
    'foshan': 'Foshan', 'fribourg': 'Fribourg', 'galway': 'Galway',
    'gent': 'Ghent', 'glasgow': 'Glasgow', 'görlitz': 'Görlitz',
    'grenoble': 'Grenoble', 'groningen': 'Groningen', 'guangzhou': 'Guangzhou',
    'guanajuato': 'Guanajuato', 'haag': 'The Hague', 'halle': 'Halle',
    'hamburg': 'Hamburg', 'helsingi': 'Helsinki', 'helsinki': 'Helsinki',
    'heerlen': 'Heerlen', 'heilbronn': 'Heilbronn', 'hongkong': 'Hong Kong',
    'hong kong': 'Hong Kong', 'istanbul': 'Istanbul', 'jerusalem': 'Jerusalem',
    'jinan': 'Jinan', 'joensuu': 'Joensuu', 'kassel': 'Kassel',
    'klaipeda': 'Klaipėda', 'kokkola': 'Kokkola', 'kopenhaagen': 'Copenhagen',
    'copenhagen': 'Copenhagen', 'košice': 'Košice', 'köln': 'Cologne',
    'kufstein': 'Kufstein', 'la rochelle': 'La Rochelle', 'leipaja': 'Liepāja',
    'león': 'León', 'lexington': 'Lexington', 'liège': 'Liège',
    'lille': 'Lille', 'lissabon': 'Lisbon', 'london': 'London',
    'los angeles': 'Los Angeles', 'lübeck': 'Lübeck', 'lüneburg': 'Lüneburg',
    'luxembourg': 'Luxembourg', 'lyon': 'Lyon', 'macau': 'Macau',
    'manchester': 'Manchester', 'maribor': 'Maribor', 'mechelen': 'Mechelen',
    'melbourne': 'Melbourne', 'merano': 'Merano', 'metz': 'Metz',
    'mexico city': 'Mexico City', 'milano': 'Milan', 'modena': 'Modena',
    'mons': 'Mons', 'montreal': 'Montreal', 'moskva': 'Moscow',
    'münchen': 'Munich', 'nantes': 'Nantes', 'nanjing': 'Nanjing',
    'naperville': 'Naperville', 'new york': 'New York', 'nijmegen': 'Nijmegen',
    'norwich': 'Norwich', 'oslo': 'Oslo', 'oxford': 'Oxford', 'pariis': 'Paris',
    'paris': 'Paris', 'penarth': 'Penarth', 'perth': 'Perth',
    'peterburi': 'Saint Petersburg', 'philadelphia': 'Philadelphia',
    'pisa': 'Pisa', 'poznan': 'Poznań', 'praha': 'Prague', 'reykjavik': 'Reykjavík',
    'reims': 'Reims', 'riia': 'Riga', 'riga': 'Riga', 'rooma': 'Rome',
    'rovigo': 'Rovigo', 'salzburg': 'Salzburg', 'saarbrücken': 'Saarbrücken',
    'são paulo': 'São Paulo', 'sarasota': 'Sarasota', 'seoul': 'Seoul',
    'shanghai': 'Shanghai', 'st. louis': 'St. Louis', 'stockholm': 'Stockholm',
    'stuttgart': 'Stuttgart', 'sydney': 'Sydney', 'tampere': 'Tampere',
    'tel aviv': 'Tel Aviv', 'tel-aviv': 'Tel Aviv', 'tilburg': 'Tilburg',
    'torino': 'Turin', 'toronto': 'Toronto', 'tournai': 'Tournai',
    'trento': 'Trento', 'trondheim': 'Trondheim', 'tucson': 'Tucson',
    'turu': 'Turku', 'turku': 'Turku', 'ulm': 'Ulm', 'utrecht': 'Utrecht',
    'uster': 'Uster', 'vaasa': 'Vaasa', 'valletta': 'Valletta',
    'valmiera': 'Valmiera', 'varese': 'Varese', 'versailles': 'Versailles',
    'viin': 'Vienna', 'vienna': 'Vienna', 'vilnius': 'Vilnius',
    'washington': 'Washington', 'wiesbaden': 'Wiesbaden', 'wrocław': 'Wrocław',
    'wroclaw': 'Wrocław', 'wuhan': 'Wuhan', 'xuzhou': 'Xuzhou',
    'zadar': 'Zadar', 'zagreb': 'Zagreb', 'zürich': 'Zurich',
    # Estonia
    'haapsalu': 'Haapsalu', 'harju-madise': 'Harju-Madise', 'jõgeva': 'Jõgeva',
    'jõhvi': 'Jõhvi', 'juuru': 'Juuru', 'kanepi': 'Kanepi', 'kärdla': 'Kärdla',
    'käina': 'Käina', 'kohtla-järve': 'Kohtla-Järve', 'kose': 'Kose',
    'kuressaare': 'Kuressaare', 'laulasmaa': 'Laulasmaa', 'lüganuse': 'Lüganuse',
    'mooste': 'Mooste', 'muhu': 'Muhu', 'naissaar': 'Naissaar', 'narva': 'Narva',
    'noarootsi': 'Noarootsi', 'paide': 'Paide', 'pärnu': 'Pärnu',
    'põlva': 'Põlva', 'rakvere': 'Rakvere', 'rapla': 'Rapla',
    'saaremaa': 'Saaremaa', 'sillamäe': 'Sillamäe', 'suure-jaani': 'Suure-Jaani',
    'tallinn': 'Tallinn', 'tallinna': 'Tallinn', 'tamsalu': 'Tamsalu',
    'tapa': 'Tapa', 'tartu': 'Tartu', 'triigi': 'Triigi', 'türi': 'Türi',
    'valga': 'Valga', 'vastseliina': 'Vastseliina', 'viimsi': 'Viimsi',
    'viinistu': 'Viinistu', 'viljandi': 'Viljandi', 'vormsi': 'Vormsi',
    'võru': 'Võru', 'vändra': 'Vändra',
}

VENUE_CITIES = {
    'estonia kontserdisaal': 'Tallinn', 'estonia kammersaal': 'Tallinn',
    'niguliste': 'Tallinn', 'mustpeade maja': 'Tallinn', 'metodisti kirik': 'Tallinn',
    'kaarli kirik': 'Tallinn', 'oleviste kirik': 'Tallinn',
    'pirita kloostri': 'Tallinn', 'emta kontserdi': 'Tallinn',
    'eesti muusika- ja teatriakadeemia': 'Tallinn', 'rahvusooper estonia': 'Tallinn',
    'vanemuise kontserdi': 'Tartu', 'tartu ülikooli aula': 'Tartu',
    'pärnu kontserdimaja': 'Pärnu', 'arvo pärdi keskus': 'Laulasmaa',
    'royal albert hall': 'London', 'usher hall': 'Edinburgh',
    'hong kong city hall': 'Hong Kong', 'grand théâtre de luxembourg': 'Luxembourg',
}

ESTONIAN_CITIES = {
    'Haapsalu', 'Harju-Madise', 'Jõgeva', 'Jõhvi', 'Juuru', 'Kanepi', 'Kärdla',
    'Käina', 'Kohtla-Järve', 'Kose', 'Kuressaare', 'Laulasmaa', 'Lüganuse',
    'Mooste', 'Muhu', 'Naissaar', 'Narva', 'Noarootsi', 'Paide', 'Pärnu',
    'Põlva', 'Rakvere', 'Rapla', 'Saaremaa', 'Sillamäe', 'Suure-Jaani',
    'Tallinn', 'Tamsalu', 'Tapa', 'Tartu', 'Triigi', 'Türi', 'Valga',
    'Vastseliina', 'Viimsi', 'Viinistu', 'Viljandi', 'Vormsi', 'Võru', 'Vändra',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+kell\s+(\d{1,2})[:.]([0-5]\d))?',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        event_date = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None
    event_time = None
    if match.group(4):
        hour = int(match.group(4))
        if hour > 23:
            return None
        event_time = f'{hour:02d}:{match.group(5)}'
    return event_date.isoformat(), event_time


def resolve_location(value):
    venue = clean_text(value)
    # Remove ticket/lecture annotations that occasionally leaked into the old
    # location field, while preserving the actual venue portion.
    venue = re.sub(r'\s+[–-]\s*(?:JÄÄB ÄRA|TASUTA!?)\s*$', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r'^(?:Kell\s+\d{1,2}[.:]\d{2}[^!]*!\s*)', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r'\.?\s*(?:Toomas Siitani )?sissejuhatav loeng.*$', '', venue, flags=re.IGNORECASE)
    if not venue:
        return None

    lowered = venue.lower()
    country_code = None
    for alias, code in COUNTRIES.items():
        if re.search(rf'(?<!\w){re.escape(alias)}(?!\w)', lowered):
            country_code = code
            break

    city = None
    for alias, resolved in CITY_ALIASES.items():
        if alias in lowered:
            city = resolved
            break
    if city is None:
        for alias, resolved in VENUE_CITIES.items():
            if alias in lowered:
                city = resolved
                break
    if city is None:
        return None

    # Foreign evidence wins over the home-country default.  If a known city
    # occurs without a country label, infer only countries with unambiguous
    # city names; otherwise an Estonian city is required.
    if country_code is None:
        if city in ESTONIAN_CITIES:
            country_code = 'EE'
        else:
            city_countries = {
                'London': 'GB', 'Edinburgh': 'GB', 'Hong Kong': 'HK',
                'Luxembourg': 'LU', 'Amsterdam': 'NL', 'Dublin': 'IE',
                'Helsinki': 'FI', 'Paris': 'FR', 'Berlin': 'DE', 'Moscow': 'RU',
                'Saint Petersburg': 'RU', 'New York': 'US', 'Washington': 'US',
                'Los Angeles': 'US', 'Toronto': 'CA', 'Sydney': 'AU',
            }
            country_code = city_countries.get(city)
    if country_code is None:
        return None
    return venue, city, country_code


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.post-type-concert')
    title = clean_text(soup.select_one('h1'))
    if article is None or not title:
        return []
    description = clean_text(article.select_one('.article-body')) or None
    records = []
    for occurrence in article.select('.concert-location'):
        parsed_datetime = parse_datetime(clean_text(occurrence.select_one('.date-time')))
        location = resolve_location(occurrence.select_one('h3'))
        if not parsed_datetime or not location:
            continue
        event_date, event_time = parsed_datetime
        # The modern archive begins in 2003. A handful of migrated posts carry
        # WordPress's 1970 placeholder date; they are not real occurrences.
        if event_date < '2003-01-01':
            continue
        venue, city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


class EpccEeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='epcc_ee',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='EE',
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

        # The stable first-party year selector exposes the complete 2003+
        # archive.  The plain calendar is also needed because it extends into
        # the next year before that archive option exists.
        urls = set()
        listing_requests = [(CONCERTS_URL, None)] + [
            (CONCERTS_URL, {'y': year}) for year in range(2003, date.today().year + 1)
        ]
        for listing_url, params in listing_requests:
            try:
                response = get_response(session, listing_url, params=params)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch EPCC concert listing',
                    event='crawler_fetch_failed', level='error', url=listing_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                raise
            soup = BeautifulSoup(response.text, 'html.parser')
            urls.update(urljoin(SOURCE_URL, link['href']) for link in soup.select('a.concert[href]'))

        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(get_response, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(future.result().text, url))
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape EPCC concert detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ))


def main():
    EpccEeCrawler().run()


if __name__ == '__main__':
    main()
