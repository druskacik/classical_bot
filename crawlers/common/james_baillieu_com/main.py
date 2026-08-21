import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://james-baillieu.com/'
SOURCE = 'James Baillieu'
PAST_EVENTS_URL = urljoin(SOURCE_URL, '?past_events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_NAMES = {
    'australia': 'AU', 'austria': 'AT', 'belgium': 'BE', 'canada': 'CA',
    'china': 'CN', 'croatia': 'HR', 'czech republic': 'CZ', 'denmark': 'DK',
    'england': 'GB', 'finland': 'FI', 'france': 'FR', 'germany': 'DE',
    'hong kong': 'HK', 'hungary': 'HU', 'ireland': 'IE', 'isle of man': 'IM',
    'italy': 'IT', 'japan': 'JP', 'luxembourg': 'LU', 'netherlands': 'NL',
    'new zealand': 'NZ', 'norway': 'NO', 'poland': 'PL', 'portugal': 'PT',
    'scotland': 'GB', 'south africa': 'ZA', 'spain': 'ES', 'sweden': 'SE',
    'switzerland': 'CH', 'taiwan': 'TW', 'uk': 'GB', 'united kingdom': 'GB',
    'usa': 'US', 'united states': 'US', 'wales': 'GB',
}

# The diary often names a famous hall or festival followed only by its country.
# These stable first-party labels let us avoid treating a country or county as a city.
VENUE_CITIES = {
    'aldeburgh festival': 'Aldeburgh',
    'auditori de girona': 'Girona',
    'auditorium du louvre': 'Paris',
    'bregenz festival': 'Bregenz',
    'carnegie hall': 'New York',
    'champs hill': 'Pulborough',
    'cologne philharmonie': 'Cologne',
    'elbphilharmonie hamburg': 'Hamburg',
    'edinburgh international festival': 'Edinburgh',
    'glynde place': 'Glynde',
    'heidelberger fruhling': 'Heidelberg',
    'heidelberger frühling': 'Heidelberg',
    'holkam hall': 'Wells-next-the-Sea',
    'kaohsiung festival': 'Kaohsiung',
    'kulturzentrum gustav mahler toblach': 'Toblach',
    'laeiszhalle': 'Hamburg',
    'lied basel': 'Basel',
    'liedbasel': 'Basel',
    'musikverein': 'Vienna',
    'oxford song festival': 'Oxford',
    'palais garnier': 'Paris',
    'palau de la música': 'Barcelona',
    'philharmonie luxembourg': 'Luxembourg',
    'rosendal festival': 'Rosendal',
    'schlossfestspiele ludwigsburg': 'Ludwigsburg',
    'schubert club': 'Saint Paul',
    'snape maltings': 'Snape',
    'saffron hall': 'Saffron Walden',
    "st john's smith square": 'London',
    "st peter's eaton square": 'London',
    'stern auditorium': 'New York',
    'tokyo spring festival': 'Tokyo',
    'two moors fesitval': 'Dartmoor',
    'two moors festival': 'Dartmoor',
    'verbier festival': 'Verbier',
    'wiener konzerthaus': 'Vienna',
    'wiener musikverein': 'Vienna',
    'wigmore hall': 'London',
    'wells maltings': 'Wells-next-the-Sea',
    'zurich opera house': 'Zurich',
}

NON_CITY_REGIONS = {'cumbria', 'devon', 'flanders', 'norfolk', 'sussex', 'west sussex'}

CITY_COUNTRIES = {
    'amsterdam': 'NL', 'antwerp': 'BE', 'barcelona': 'ES', 'basel': 'CH', 'bergen': 'NO',
    'berlin': 'DE', 'birmingham': 'GB', 'bregenz': 'AT', 'bristol': 'GB',
    'brussels': 'BE', 'cardiff': 'GB', 'cologne': 'DE', 'dortmund': 'DE',
    'dublin': 'IE', 'edinburgh': 'GB', 'geneva': 'CH', 'girona': 'ES',
    'gateshead': 'GB', 'graz': 'AT', 'hamburg': 'DE', 'hannover': 'DE',
    'heidelberg': 'DE', 'ingolstadt': 'DE', 'kendal': 'GB', 'kiel': 'DE',
    'kaohsiung': 'TW', 'london': 'GB', 'los angeles': 'US',
    'luxembourg': 'LU', 'madrid': 'ES', 'minneapolis': 'US', 'munich': 'DE',
    'lisbon': 'PT', 'liverpool': 'GB', 'new york': 'US', 'oxford': 'GB',
    'paris': 'FR', 'perth': 'GB', 'pisa': 'IT', 'porto': 'PT',
    'portland': 'US', 'portsmouth': 'GB', 'princeton': 'US',
    'saint paul': 'US', 'san francisco': 'US', 'saffron walden': 'GB',
    'seattle': 'US', 'snape': 'GB', 'toblach': 'IT', 'tokyo': 'JP',
    'toronto': 'CA', 'vancouver': 'CA', 'verbier': 'CH', 'vienna': 'AT',
    'washington': 'US', 'zurich': 'CH',
}


def clean_text(value):
    text = (value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date_and_time(value):
    date_match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
        r'(\d{1,2})\s+([A-Za-z]+).*?(\d{4})',
        value,
        re.I,
    )
    if not date_match:
        return None, None
    try:
        event_date = datetime.strptime(
            ' '.join(date_match.groups()), '%d %B %Y'
        ).date().isoformat()
    except ValueError:
        return None, None

    time_match = re.search(r'\b(\d{1,2})[.:](\d{2})\s*(am|pm)?\b', value, re.I)
    if not time_match:
        return event_date, None
    hour, minute = int(time_match.group(1)), int(time_match.group(2))
    meridiem = (time_match.group(3) or '').lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return event_date, None
        hour = hour % 12 + (12 if meridiem == 'pm' else 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return event_date, None
    return event_date, f'{hour:02d}:{minute:02d}'


def _lookup_venue_city(venue):
    folded = clean_text(venue).casefold()
    for label, city in VENUE_CITIES.items():
        if label in folded:
            return city
    return None


def _city_from_parts(parts):
    for part in reversed(parts):
        folded = part.casefold()
        if folded in CITY_COUNTRIES:
            return part
        for known_city in CITY_COUNTRIES:
            if re.search(rf'\b{re.escape(known_city)}\b', folded):
                return known_city.title()
    return _lookup_venue_city(parts[0])


def parse_location(value):
    location = clean_text(value)
    if not location:
        return None
    parts = [clean_text(part) for part in re.split(r'\s*[,/]\s*', location) if clean_text(part)]
    if not parts:
        return None

    country_code = None
    if parts[-1].casefold() in COUNTRY_NAMES:
        country_code = COUNTRY_NAMES[parts.pop().casefold()]

    if not parts:
        return None
    venue = parts[0]
    city = _city_from_parts(parts)
    if not city and country_code and len(parts) >= 2:
        city = parts[-1]
    if city and city.casefold() in NON_CITY_REGIONS:
        return None
    if not city or city.casefold() == venue.casefold():
        return None

    inferred_country = CITY_COUNTRIES.get(city.casefold())
    country_code = country_code or inferred_country
    if not country_code:
        return None
    return venue, city, country_code


def parse_row(row, listing_url):
    cells = row.find_all('td', recursive=False)
    if len(cells) < 3:
        return None
    date_text = clean_text(cells[0].get_text(' ', strip=True))
    location_text = clean_text(cells[1].get_text(' ', strip=True))
    description = clean_text(cells[2].get_text('\n', strip=True))
    event_date, time_from = parse_date_and_time(date_text)
    location = parse_location(location_text)
    if not event_date or not description or not location:
        return None

    link = row.select_one('a[href]')
    event_url = urljoin(listing_url, link['href']) if link else f'{listing_url}#events'
    venue, city, country_code = location
    return {
        'title': description,
        'date': event_date,
        'url': event_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    seen = set()
    for listing_url in (SOURCE_URL, PAST_EVENTS_URL):
        log_message('Fetching James Baillieu event listing', event='crawler_url_fetch', url=listing_url)
        response = session.get(listing_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for row in soup.select('table tbody tr'):
            record = parse_row(row, listing_url)
            if not record:
                continue
            key = (record['title'], record['date'], record['time_from'], record['venue'])
            if key not in seen:
                seen.add(key)
                records.append(record)

    log_message(
        'James Baillieu event scrape completed',
        event='crawler_scrape_completed',
        record_count=len(records),
    )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class JamesBaillieuComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='james_baillieu_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    JamesBaillieuComCrawler().run()


if __name__ == '__main__':
    main()
