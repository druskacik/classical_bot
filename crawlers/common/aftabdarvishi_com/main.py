import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://aftabdarvishi.com/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'news/')
SOURCE = 'Aftab Darvishi'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_NAMES = {
    'AT': 'AT', 'Austria': 'AT', 'BE': 'BE', 'Belgium': 'BE',
    'Bosnia Herzegovina': 'BA', 'DE': 'DE', 'Germany': 'DE',
    'FR': 'FR', 'Fr': 'FR', 'France': 'FR', 'IT': 'IT', 'Italy': 'IT',
    'NL': 'NL', 'Netherlands': 'NL', 'The Netherlands': 'NL',
    'Sweden': 'SE', 'UK': 'GB', 'United States': 'US',
}

# The archive is a touring composer's calendar. These mappings are limited to
# locations explicitly evidenced by the archive text or the named venue.
CITY_COUNTRIES = {
    'Alkmaar': 'NL', 'Alphen a/d Rijn': 'NL', 'Amersfoort': 'NL',
    'Amsterdam': 'NL', 'Angers': 'FR', 'Apeldoorn': 'NL', 'Arnhem': 'NL',
    'Berlin': 'DE', 'Breda': 'NL', 'Cholet': 'FR', 'Coesfeld': 'DE',
    'Dalfsen': 'NL', 'Den Bosch': 'NL', 'Den Haag': 'NL', 'Dinxperlo': 'NL',
    'Dordrecht': 'NL', 'Doetinchem': 'NL', 'Dortmund': 'DE',
    'Dusseldorf': 'DE', 'Düsseldorf': 'DE', 'Eindhoven': 'NL',
    'Enschede': 'NL', 'Haarlem': 'NL', 'Hamilton': 'US', 'Hardenberg': 'NL',
    'Leiden': 'NL', 'Leipzig': 'DE', 'Leuven': 'BE', 'London': 'GB',
    'Lund': 'SE', 'Maastricht': 'NL', 'Middelburg': 'NL', 'Muiderberg': 'NL',
    'Munich': 'DE', 'Nantes': 'FR', 'New York': 'US', 'Nijmegen': 'NL',
    'Noordwijk': 'NL', 'Oldenzaal': 'NL', 'Ohio': 'US', 'Oosterbeek': 'NL',
    'Paris': 'FR', 'Raalte': 'NL', 'Rochester': 'US', 'San Francisco': 'US',
    'Stockholm': 'SE', 'Tilburg': 'NL', 'Utrecht': 'NL', 'Verona': 'IT',
    'Vlissingen': 'NL', 'Washington': 'US', 'Wien': 'AT', 'Woerden': 'NL',
}

VENUE_CITIES = {
    'Alte Schmiede Wien Musikwerkstatt': 'Wien',
    'Amstelkerk': 'Amsterdam', 'Barbican': 'London', 'Bimhuis': 'Amsterdam',
    'Carnegie Hall': 'New York', 'Chassé Theater & Cinema': 'Breda',
    'Concertgebouw De Vereeniging': 'Nijmegen', 'De Doelen': 'Rotterdam',
    'De Harmonie': 'Leeuwarden', 'DE LINK': 'Tilburg',
    'Energiehuis – Machine 3': 'Dordrecht', 'EventTheater Concertzaal': 'Oosterbeek',
    'Fort Uitermeer': 'Weesp', 'Gewandhauses': 'Leipzig',
    'Grote Kerk': 'Dalfsen', 'Grote of Sint-Jacobskerk': 'Vlissingen',
    'Hamilton College': 'Hamilton', 'Het Klooster Theater': 'Woerden',
    'HOFtheater': 'Raalte', 'Johanneskirche': 'Dusseldorf',
    'Jheronimus Bosch Art Center': 'Den Bosch', 'Kerkje Valkkoog': 'Valkkoog',
    'Koln Philharmonie': 'Cologne', 'Konserthuset Stockholm': 'Stockholm',
    'Konzerthaus Dortmund': 'Dortmund', 'Konzert Theater Coesfeld': 'Coesfeld',
    'Korzo': 'Den Haag', 'Leidse Schouwburg': 'Leiden',
    'Lindenberg': 'Nijmegen', 'Lokhorstkerk': 'Leiden',
    'Lund University': 'Lund', 'Meervaart': 'Amsterdam', 'Muziekgebouw Amsterdam': 'Amsterdam',
    'Muziekgebouw aan ’t IJ': 'Amsterdam', "Muziekgebouw aan ‘t IJ": 'Amsterdam',
    'Muziekgebouw': 'Amsterdam', 'Muziekcentrum': 'Enschede', 'Musis Arnhem': 'Arnhem',
    'Musis Sacrum': 'Arnhem', 'Musis': 'Arnhem', 'Nieuwe kerk': 'Den Haag',
    'Oosterpoort': 'Groningen', 'Orgelpark': 'Amsterdam', 'Orpheus': 'Apeldoorn',
    'Philharmonie de Paris': 'Paris', 'Philharmonie': 'Haarlem', 'Phil': 'Haarlem',
    'Podium Klassiek Eindhoven': 'Eindhoven', 'Schouwburg Amphion': 'Doetinchem',
    'Short North Stage': 'Ohio', 'Splendor': 'Amsterdam',
    'St John’s Smith Square': 'London', 'Stadtheater de Bond': 'Oldenzaal',
    'Stevenskerk': 'Nijmegen', 'Sveriges Radio Berwaldhallen': 'Stockholm',
    'TAQA Theater de Vest': 'Alkmaar', 'Teatro Ristori': 'Verona',
    'Theaters Tilburg': 'Tilburg', 'Tivoli Vredenburg': 'Utrecht',
    'Tonhalle': 'Düsseldorf', 'Willem Twee Toonzaal': 'Den Bosch',
    'Willen Twee Toonzaal': 'Den Bosch', 'Wilminktheater': 'Enschede',
}

CITY_COUNTRIES.update({
    'Cologne': 'DE', 'Groningen': 'NL', 'Leeuwarden': 'NL', 'Rotterdam': 'NL',
    'Valkkoog': 'NL', 'Weesp': 'NL',
})


def clean_text(value):
    if not value:
        return ''
    text = ' '.join(str(value).replace('\xa0', ' ').split())
    return re.sub(r'\s+([,.;])', r'\1', text).strip()


def event_date(article):
    month = clean_text(article.select_one('.news-item_date-container_month').get_text())
    day = clean_text(article.select_one('.news-item_date-container_day').get_text())
    year = clean_text(article.select_one('.news-item_date-container_year').get_text())
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def find_location(text):
    lowered = text.casefold()
    venue = city = country_code = None
    location_text = text
    marker = list(re.finditer(r'\b(?:at|in)\s+', text, re.I))
    if marker:
        location_text = text[marker[-1].end():]

    for candidate, candidate_city in sorted(
        VENUE_CITIES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if candidate.casefold() in lowered:
            venue, city = candidate, candidate_city
            break

    if not city:
        for candidate, code in sorted(
            CITY_COUNTRIES.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if re.search(rf'(?<!\w){re.escape(candidate)}(?!\w)', location_text, re.I):
                city, country_code = candidate, code
                break

    if not country_code:
        for label, code in sorted(COUNTRY_NAMES.items(), key=lambda item: len(item[0]), reverse=True):
            if re.search(rf'(?:,|[-–])\s*{re.escape(label)}\.?\s*$', text, re.I):
                country_code = code
                break
    if city and not country_code:
        country_code = CITY_COUNTRIES.get(city)

    if not venue and city:
        match = re.search(r'\bat\s+(.+)$', text, re.I)
        if match:
            venue = match.group(1)
            venue = re.sub(rf'\s*[,\-–]\s*{re.escape(city)}\b.*$', '', venue, flags=re.I)
            for label in sorted(COUNTRY_NAMES, key=len, reverse=True):
                venue = re.sub(rf'\s*[,\-–]\s*{re.escape(label)}\.?\s*$', '', venue, flags=re.I)
            venue = clean_text(venue).strip(' ,-–.')

    if venue and re.search(r'\b(?:festival|biennale|classical next|series)\b', venue, re.I):
        venue = None

    if not venue or not city or not country_code or venue.casefold() == city.casefold():
        return None, None, None
    return venue, city, country_code


def parse_article(article):
    content = article.select_one('.news-item__text')
    title = clean_text(content.get_text(' ', strip=True) if content else '')
    parsed_date = event_date(article)
    venue, city, country_code = find_location(title)
    if not title or not parsed_date or not venue or not city or not country_code:
        return None
    link = content.select_one('a[href]') if content else None
    fallback_url = f'{ARCHIVE_URL}#{article.get("id")}' if article.get('id') else ARCHIVE_URL
    url = urljoin(ARCHIVE_URL, link.get('href')) if link else fallback_url
    return {
        'title': title,
        'date': parsed_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': title,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AftabDarvishiComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aftabdarvishi_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(ARCHIVE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('article.news-item')
        records = []
        for article in articles:
            record = parse_article(article)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Aftab Darvishi archive item',
                    event='crawler_item_skipped',
                    level='warning',
                    url=f'{ARCHIVE_URL}#{article.get("id", "unknown")}',
                    error_type='IncompleteEventData',
                    error_message='Required date, title, venue, city, or country is missing',
                )
        if not articles:
            log_message(
                'No Aftab Darvishi archive items found',
                event='crawler_empty_source',
                level='warning',
                url=ARCHIVE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['title'], item['venue']))


def main():
    AftabDarvishiComCrawler().run()


if __name__ == '__main__':
    main()
