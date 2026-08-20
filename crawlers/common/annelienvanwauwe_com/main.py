import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://annelienvanwauwe.com/'
TOUR_URL = urljoin(SOURCE_URL, 'tour/')
SOURCE = 'Annelien Van Wauwe'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The artist tours internationally. The site supplies a city rather than a
# structured country field, so countries are resolved from its recurring city
# vocabulary. Parenthesised country hints on older entries take precedence.
COUNTRY_HINTS = {
    'BE': {'be', 'belgium'}, 'CA': {'ca', 'canada'}, 'CH': {'ch'},
    'CN': {'cn'}, 'DE': {'d', 'de', 'germany'}, 'GB': {'uk'},
    'IT': {'it'}, 'KR': {'kr'}, 'NL': {'nl'}, 'NO': {'no'}, 'JP': {'jp'},
}

COUNTRY_CITIES = {
    'AT': {'graz', 'linz', 'salzburg'},
    'BE': {
        'antwerp', 'antwerpen', 'bierbeek', 'blankenberge', 'bornem', 'bruges',
        'brugge', 'brussel', 'brussels', 'charleroi', 'damme', 'de pinte',
        'dendermonde', 'eeklo', 'ekeren', 'genk', 'gent', 'ghent', 'grimbergen',
        'hamme', 'hasselt', 'keerbergen', 'kortrijk', 'leuven', 'leut', 'liège',
        'lier', 'limburg', 'lokeren', 'lommel', 'maasmechelen', 'mechelen', 'mol',
        'mortsel', 'roeselare', 'sint-niklaas', 'sint-truiden', 'tielt', 'turnhout',
        'waasmunster', 'willebroek'},
    'CA': {'charlevoix'},
    'CH': {'gersau', 'rapperswil', 'zurich', 'zürich'},
    'CN': {'chengdu'},
    'DE': {
        'aachen', 'ahaus', 'altenburg', 'aschaffenburg', 'bad cannstatt', 'berlin',
        'böblingen', 'bonn', 'bordesholm', 'braunschweig', 'bruchsal', 'chemnitz',
        'coburg', 'dortmund', 'dresden', 'düsseldorf', 'essen', 'flensburg',
        'frankfurt', 'gütersloh', 'hamburg', 'hamm', 'hannover', 'heidelberg',
        'hersbruck', 'hitzacker', 'hof', 'husum', 'ingolstadt', 'itzehoe', 'jena',
        'kleve', 'koblenz', 'kreischa', 'kulmbach', 'lüdenscheid', 'malgarten',
        'monheim am rhein', 'munich', 'münster', 'nürnberg', 'osnabrück', 'plauen',
        'rendsburg', 'saarbrücken', 'sande', 'schleswig', 'sendenhorst', 'siegburg',
        'steinfurt', 'usingen', 'weilburg', 'wiesbaden', 'würzburg', 'zwickau'},
    'DK': {'hindsgavl'},
    'DO': {'santo-domingo'},
    'ES': {'barcelona', 'las palmas', 'malaga', 'palma de mallorca', 'tenerifa',
           'vitoria-gasteiz'},
    'FR': {'cannes', 'fontainebleau', 'fontenay-sous-bois', 'gargilesse',
           'la chaise dieu', 'lille', 'meaux', 'nancy', 'paris', 'pontarlier'},
    'GB': {'aldeburgh', 'bath', 'belfast', 'birmingham', 'bodmin', 'bournemouth',
           'cambridge', 'cardiff', 'cheltenham festival: pittville pump room',
           'guiting power', 'harrogate', 'inverness', 'london', 'manchester',
           'norfolk & norwich festival', 'pickering', 'richmond', 'ryedale',
           'southampton', 'stratford-on-avon festival', 'swansea', 'truro'},
    'HU': {'budapest'},
    'IE': {'bantry', "festiv'ards"},
    'IN': {'mumbai'},
    'IT': {'classicheforme festival', 'milano'},
    'JP': {'tokyo'},
    'KR': {'incheon', 'seoul'},
    'LV': {'liepāja', 'riga'},
    'LU': {'ettelbrück', 'luxemburg'},
    'MX': {'mexico city'},
    'MY': {'kuala lumpur'},
    'NL': {'amsterdam', 'apeldoorn', 'arnhem', 'breda', 'bussem', 'den haag',
           'doetinchem', 'ede', 'eindhoven', 'enschede', 'geldrop', 'haarlem',
           'heerlen', 'kerkrade', 'middelburg', 'nieuwkoop', 'nijmegen',
           'noordgauwe', 'noordgouwe', 'rotterdam', 'the hague', 'tilburg',
           'utrecht', 'zeist', 'zierikzee'},
    'NO': {'bergen', 'oslo'},
    'NZ': {'auckland'},
    'PL': {'gdansk', 'katowice', 'rzeszów', 'szczecin', 'wroclaw'},
    'PT': {'lisbon'},
    'SE': {'malmö'},
    'ZA': {'johannesburg', 'pretoria'},
    'US': {'reno nevada'},
}

VENUE_WORDS = re.compile(
    r'(?:academy|abbey|amuz|aurora|barn|bijloke|bozar|brucknerhaus|castle|cathedral|cc|'
    r'center|centre|centrum|church|college|concertgebouw|concert hall|concertzaal|'
    r'congresshalle|conservator|cultureel|domaine forget|eglise|église|eurogress|'
    r'festsaal|flagey|guildhall|hall|haus|havichhorst|hfm|hochschule|kerk|kirche|'
    r'klosterkirche|konzerthaus|kulturrafinerie|kunsthumaniora|museum|'
    r'muziekcentrum|muziekgebouw|opera|palace|pavilion|philharmoni|pump room|'
    r'residenz|rodahal|saal|school|skolen|schloss|schouwburg|singel|'
    r'stadsschouwburg|stadttheater|studio|theater|theatre|tivoli vredenburg|'
    r'tonhalle|university|wintercircus|zaal)\b', re.I,
)


def clean_lines(element):
    if element is None:
        return []
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip()]


def parse_datetime(value):
    value = re.sub(r'\s+', ' ', value).strip()
    for pattern in ('%d %B, %Y, %I.%M %p', '%d %B, %Y'):
        try:
            parsed = datetime.strptime(value, pattern)
            time_from = parsed.strftime('%H:%M') if '%I' in pattern else None
            # Midnight is used by this site as an unknown-time placeholder.
            if time_from == '00:00':
                time_from = None
            return parsed.date().isoformat(), time_from
        except ValueError:
            pass
    return None, None


def parse_place(value):
    value = re.sub(r'\s+', ' ', value).strip()
    hint_match = re.search(r'\(([^)]+)\)', value)
    hint = hint_match.group(1).strip().casefold() if hint_match else ''
    value = re.sub(r'\s*\([^)]+\)\s*', '', value).strip()
    parts = [part.strip() for part in value.split(',', 1)]
    city = parts[0]
    if not city or city.casefold() in {'world', 'tour', 'release'}:
        return None

    country_code = next(
        (code for code, hints in COUNTRY_HINTS.items() if hint in hints), None
    ) if hint else None
    if country_code is None:
        key = city.casefold()
        country_code = next(
            (code for code, cities in COUNTRY_CITIES.items() if key in cities), None
        )
    if country_code is None:
        return None

    embedded_venue = parts[1] if len(parts) == 2 else None
    return city, country_code, embedded_venue


def select_venue(embedded_venue, detail_lines):
    if embedded_venue and VENUE_WORDS.search(embedded_venue):
        return embedded_venue, detail_lines
    for index, line in enumerate(detail_lines[:3]):
        if VENUE_WORDS.search(line):
            return line, detail_lines[:index] + detail_lines[index + 1:]
    return None, detail_lines


def parse_article(article):
    columns = article.find_all('div', recursive=False)
    if len(columns) < 2:
        return None
    heading_lines = clean_lines(columns[0].select_one('p'))
    detail_lines = clean_lines(columns[1].select_one('p'))
    if len(heading_lines) < 2 or not detail_lines:
        return None

    event_date, time_from = parse_datetime(heading_lines[0])
    place = parse_place(heading_lines[1])
    if not event_date or not place:
        return None
    city, country_code, embedded_venue = place
    venue, description_lines = select_venue(embedded_venue, detail_lines)
    if not venue:
        return None

    description = '\n'.join(detail_lines)
    title_lines = description_lines or detail_lines
    title = title_lines[0] if title_lines else ''
    if not title or title.casefold() == venue.casefold():
        title = f'Annelien Van Wauwe in {city}'

    link = article.select_one('a.more[href], div:first-child a[href]')
    url = urljoin(TOUR_URL, link['href']) if link else TOUR_URL
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AnnelienVanWauweComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='annelienvanwauwe_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(TOUR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Annelien Van Wauwe tour page',
                event='crawler_fetch_failed',
                level='error',
                url=TOUR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('article.perfo-item')
        if not articles:
            raise ValueError('No tour entries found on the tour page')

        records = []
        for article in articles:
            record = parse_article(article)
            if record:
                records.append(record)
        log_message(
            'Parsed Annelien Van Wauwe tour entries',
            event='crawler_items_parsed',
            url=TOUR_URL,
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    AnnelienVanWauweComCrawler().run()


if __name__ == '__main__':
    main()
