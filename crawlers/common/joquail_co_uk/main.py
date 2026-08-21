import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.joquail.co.uk/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts/')
SOURCE = 'Jo Quail'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The calendar is an international tour archive. These places occur in its
# free-text listings; a record is emitted only where both a city and a distinct
# venue can be recovered. Longer names must be tested first.
CITY_COUNTRIES = {
    'Ponta do Sol': 'PT', 'Dampierre-les-Bois': 'FR', 'Sint-Niklaas': 'BE',
    'Saffron Walden': 'GB', 'Königstein': 'DE', 'Bielsko-Biała': 'PL',
    'Kuala Lumpur': 'MY', 'New York': 'US', 'Los Angeles': 'US',
    'San Francisco': 'US', 'Mexico City': 'MX', 'Buenos Aires': 'AR',
    'Salisbury': 'GB', 'Meifod': 'GB', 'Glossop': 'GB', 'Leicester': 'GB',
    'Colchester': 'GB', 'Utrecht': 'NL', 'Rouen': 'FR', 'London': 'GB',
    'Allazi': 'LV', 'Riga': 'LV', 'Bristol': 'GB', 'Clisson': 'FR',
    'Cork': 'IE', 'Dublin': 'IE', 'Haacht': 'BE', 'Baiersdorf': 'DE',
    'Prague': 'CZ', 'Krakow': 'PL', 'Wroclaw': 'PL', 'Warsaw': 'PL',
    'Tallinn': 'EE', 'Helsinki': 'FI', 'Stockholm': 'SE', 'Oslo': 'NO',
    'Copenhagen': 'DK', 'Hamburg': 'DE', 'Berlin': 'DE', 'Tilburg': 'NL',
    'Haarlem': 'NL', 'Budapest': 'HU', 'Zagreb': 'HR', 'Ljubljana': 'SI',
    'Rome': 'IT', 'Milan': 'IT', 'Lyon': 'FR', 'Toulouse': 'FR',
    'Barcelona': 'ES', 'Madrid': 'ES', 'Lisbon': 'PT', 'Porto': 'PT',
    'Southampton': 'GB', 'Nottingham': 'GB', 'Edinburgh': 'GB',
    'Glasgow': 'GB', 'Newcastle': 'GB', 'Manchester': 'GB', 'Oldenzaal': 'NL',
    'Gdansk': 'PL', 'Vilnius': 'LT', 'Ghent': 'BE', 'Zurich': 'CH',
    'Bielefeld': 'DE', 'Cologne': 'DE', 'Paris': 'FR', 'Sheffield': 'GB',
    'Leeds': 'GB', 'Worcester': 'GB', 'Swansea': 'GB', 'Gloucester': 'GB',
    'Frome': 'GB', 'Brighton': 'GB', 'Bochum': 'DE', 'Leipzig': 'DE',
    'Poznan': 'PL', 'Vienna': 'AT', 'Bologna': 'IT', 'Turin': 'IT',
    'Kufstein': 'AT', 'Trier': 'DE', 'Zottegem': 'BE', 'Bergen': 'NO',
    'Tours': 'FR', 'Montpellier': 'FR', 'Verona': 'IT', 'Liège': 'BE',
    'Groningen': 'NL', 'Aalborg': 'DK', 'Limerick': 'IE', 'Belfast': 'GB',
    'Birmingham': 'GB', 'Wiesbaden': 'DE', 'Nijmegen': 'NL',
    'Brussels': 'BE', 'Almere': 'NL', 'Geneva': 'CH', 'Munich': 'DE',
    'Luxembourg': 'LU', 'Rennes': 'FR', 'Lille': 'FR', 'Bath': 'GB',
    'Cambridge': 'GB', 'Melbourne': 'AU', 'Sydney': 'AU', 'Brisbane': 'AU',
    'Adelaide': 'AU', 'Perth': 'AU', 'Istanbul': 'TR', 'Gothenburg': 'SE',
    'Karlsruhe': 'DE', 'Arlon': 'BE', 'Belgrade': 'RS', 'Sofia': 'BG',
    'Gävle': 'SE', 'Eindhoven': 'NL', 'Manchester': 'GB',
}

COUNTRY_PREFIXES = (
    'Austria|Belgium|Croatia|Czech Republic|Denmark|Estonia|Finland|France|'
    'Germany|Hungary|Italy|Latvia|Lithuania|Netherlands|Norway|Poland|Portugal|'
    'Slovenia|Spain|Sweden|Switzerland|UK|DE|FR|PL|AT|CH|IT|BE|NL|NO'
)
STOP_RE = re.compile(
    r'\s+(?:w(?:ith)?\b|as special guest\b|headline concert\b|co-headline\b|'
    r'SOLD OUT\b|performing\b|support(?:ed)? by\b|with support\b).*$'
    , re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def find_city(text):
    aliases = {'Tallin': ('Tallinn', 'EE'), 'Köln': ('Cologne', 'DE'),
               'Milano': ('Milan', 'IT'), 'Bruxelles': ('Brussels', 'BE')}
    candidates = [(name, name, code) for name, code in CITY_COUNTRIES.items()]
    candidates.extend((alias, city, code) for alias, (city, code) in aliases.items())
    for needle, city, country in sorted(candidates, key=lambda row: -len(row[0])):
        match = re.search(rf'(?<!\w){re.escape(needle)}(?!\w)', text, re.IGNORECASE)
        if match:
            return match, city, country
    return None, None, None


def clean_venue(value):
    value = STOP_RE.sub('', value)
    value = re.sub(rf'^(?:{COUNTRY_PREFIXES})\s+', '', value, flags=re.IGNORECASE)
    value = re.sub(r'^(?:re-?scheduled date!?|new concert date!)\s*', '', value, flags=re.I)
    value = value.strip(' ,–—-')
    if not value or re.fullmatch(r'(?:headline )?concert|festival', value, re.I):
        return None
    return value


def place_from_title(title):
    # A few listings contain enough location evidence in prose rather than in
    # their usual city/venue shorthand.
    special = (
        (r'Woodford Village Hall', 'Woodford Village Hall', 'Salisbury', 'GB'),
        (r'BOSS HQ.*Denmark Street', 'BOSS HQ', 'London', 'GB'),
        (r'Allažu Evaņģēliski', 'Allažu Evaņģēliski luteriskā draudze', 'Allazi', 'LV'),
        (r'Strazdumuižas parks', 'Strazdumuižas parks', 'Riga', 'LV'),
        (r'Festung Königstein', 'Festung Königstein', 'Königstein', 'DE'),
        (r'Dans La Crypte Rosa Crux', 'La Crypte Rosa Crux', 'Rouen', 'FR'),
        (r'^Colchester Arts Centre\b', 'Colchester Arts Centre', 'Colchester', 'GB'),
        (r'^Utrecht with Aaron Stainthorpe', 'TivoliVredenburg', 'Utrecht', 'NL'),
    )
    for pattern, venue, city, country in special:
        if re.search(pattern, title, re.I):
            return venue, city, country

    match, city, country = find_city(title)
    if not match:
        return None, None, None
    before = clean_venue(title[:match.start()])
    after = clean_venue(title[match.end():])

    # Listings alternate between "venue, city" and "city venue". Prefer a
    # comma-separated left side; otherwise a meaningful right side.
    if before and (',' in title[:match.start()] or title[:match.start()].lstrip().lower().startswith('the ')):
        venue = before
    elif after:
        venue = after
    else:
        venue = before
    if venue and venue.casefold() == city.casefold():
        venue = None
    if venue and re.fullmatch(r'\(?[A-Z]{2}\)?', venue):
        venue = None
    if venue and venue.casefold() == 'counter chamber':
        venue = None
    return venue, city, country


def parse_time(text):
    range_match = re.search(r'\b(\d{1,2})\s*[–—-]\s*\d{1,2}\s*(am|pm)\b', text, re.I)
    if range_match:
        raw = f'{range_match.group(1)}:00{range_match.group(2)}'
        try:
            return datetime.strptime(raw.upper(), '%I:%M%p').strftime('%H:%M')
        except ValueError:
            return None
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', text, re.I)
    if not match:
        match = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if not match:
        return None
    raw = f"{match.group(1)}:{match.group(2) or '00'}{match.group(3) or ''}"
    try:
        fmt = '%I:%M%p' if match.group(3) else '%H:%M'
        return datetime.strptime(raw.upper(), fmt).strftime('%H:%M')
    except ValueError:
        return None


def parse_item(item, year):
    date_text = clean_text(item.select_one('.date'))
    title = clean_text(item.select_one('.title'))
    if not title or not date_text:
        return None
    try:
        date = datetime.strptime(f'{date_text} {year}', '%dth %b %Y').date()
    except ValueError:
        normalized = re.sub(r'(\d+)(?:st|nd|rd|th)', r'\1', date_text, flags=re.I)
        try:
            date = datetime.strptime(f'{normalized} {year}', '%d %b %Y').date()
        except ValueError:
            return None

    venue, city, country = place_from_title(title)
    if not venue or not city or not country:
        return None
    link = item.select_one('a[href]')
    url = urljoin(CONCERTS_URL, link['href']) if link and link['href'].startswith(('http://', 'https://')) else CONCERTS_URL
    return {
        'title': title,
        'date': date.isoformat(),
        'url': url,
        'time_from': parse_time(title),
        'venue': venue,
        'city': city,
        'country_code': country,
        'description': title,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class JoquailCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='joquail_co_uk',
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
        try:
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Jo Quail concert archive',
                event='crawler_feed_failed', level='error', url=CONCERTS_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.content, 'html.parser')
        records = []
        for section in soup.select('.year'):
            year_text = clean_text(section.select_one('h3.accordion'))
            if not re.fullmatch(r'20\d{2}', year_text):
                continue
            for item in section.select('li'):
                record = parse_item(item, int(year_text))
                if record:
                    records.append(record)
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    JoquailCoUkCrawler().run()


if __name__ == '__main__':
    main()
