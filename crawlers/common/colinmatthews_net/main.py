import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://colinmatthews.net/'
SOURCE = 'Colin Matthews'
FEED_URLS = (
    urljoin(SOURCE_URL, 'upcoming-performances/'),
    urljoin(SOURCE_URL, 'past-performances/'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
DATE_TITLE_RE = re.compile(
    r'^(\d{1,2}\s+[A-Za-z]+,?\s+\d{4})\s*[-–—]\s*(.+)$'
)

# The catalogue tours internationally and frequently omits the country. These
# first-party location strings are more reliable than assigning the composer's
# home country to every performance.
CITY_COUNTRIES = {
    'Anglet': 'FR', 'Atlanta': 'US', 'Bamberg': 'DE', 'Battersea': 'GB',
    'Belo Horizonte': 'BR', 'Berlin': 'DE', 'Birmingham': 'GB', 'Brunswick': 'US',
    'Bloomsbury': 'GB', 'Boston': 'US', 'Breda': 'NL', 'Bremen': 'DE',
    'Bucharest': 'RO', 'Cambo-les-Bains': 'FR', 'Cardiff': 'GB',
    'Chicago': 'US', 'Cincinnati': 'US', 'Dortmund': 'DE',
    'Eindhoven': 'NL',
    'Folkestone': 'GB', 'Gateshead': 'GB', 'Glasgow': 'GB', 'Granada': 'ES',
    'Greenwich': 'GB', 'Hamburg': 'DE', 'Hay-on-Wye': 'GB',
    'Huddersfield': 'GB', 'Innsbruck': 'AT', 'Kaohsiung': 'TW', 'Kemi': 'FI',
    'Koblenz': 'DE', 'Lastingham': 'GB', 'Limoges': 'FR', 'Lincoln': 'GB',
    'London': 'GB', 'Los Angeles': 'US', 'Luneburg': 'DE', 'Luxembourg': 'LU',
    'Lyon': 'FR', 'Madrid': 'ES',
    'Mainz': 'DE', 'Manchester': 'GB', 'Meiningen': 'DE', 'Milan': 'IT',
    'Montréal': 'CA', 'Montreal': 'CA', 'New York': 'US', 'Odense': 'DK',
    'Ottawa': 'CA', 'Paris': 'FR', 'Perth': 'AU', 'Pittsburgh': 'US',
    'Poole': 'GB', 'Port Erin': 'IM', 'Prague': 'CZ', 'Rome': 'IT',
    'Rovaniemi': 'FI', 'Saint-Étienne': 'FR', 'San Antonio': 'US',
    'São Paulo': 'BR', 'Seoul': 'KR', 'Sheffield': 'GB', 'Snape': 'GB',
    'Stamford': 'GB', 'Stuttgart': 'DE', 'Sydney': 'AU', 'Tokyo': 'JP',
    'Toronto': 'CA', 'Unterägeri': 'CH', 'Utrecht': 'NL', 'Vigo': 'ES',
    'Washington DC': 'US', 'West Malling': 'GB', 'Winchester': 'GB',
    'Winterthur': 'CH', 'Worcester': 'GB', 'Wandsworth': 'GB', 'Zürich': 'CH',
}
COUNTRY_NAMES = {
    'Australia': 'AU', 'Austria': 'AT', 'Brazil': 'BR', 'Canada': 'CA',
    'Denmark': 'DK', 'Finland': 'FI', 'France': 'FR', 'Germany': 'DE',
    'Isle of Man': 'IM', 'Italy': 'IT', 'Japan': 'JP', 'Luxembourg': 'LU',
    'Netherlands': 'NL', 'The Netherlands': 'NL', 'Romania': 'RO',
    'South Korea': 'KR', 'Spain': 'ES', 'Switzerland': 'CH', 'Taiwan': 'TW',
    'USA': 'US', 'United States': 'US', 'Wales': 'GB',
}
VENUE_INFERENCES = {
    'Aldeburgh Festival': ('Aldeburgh', 'GB'),
    'CBSO Centre': ('Birmingham', 'GB'),
    'Cheltenham Music Festival': ('Cheltenham', 'GB'),
    'Erin Arts Centre': ('Port Erin', 'IM'),
    'Hatfield House': ('Hatfield', 'GB'),
    'Illinois State University': ('Normal', 'US'),
    'Lagerquist Concert Hall': ('Tacoma', 'US'),
    'Lichfield Festival': ('Lichfield', 'GB'),
    'Musikkollegium Winterthur': ('Winterthur', 'CH'),
    'National Kaohsiung Center': ('Kaohsiung', 'TW'),
    'Orford Church': ('Orford', 'GB'),
    'Opéra National de Bordeaux': ('Bordeaux', 'FR'),
    'Pacific Lutheran University': ('Tacoma', 'US'),
    'Philharmonie Luxembourg': ('Luxembourg', 'LU'),
    'Royal Academy of Music': ('London', 'GB'),
    'Royal Birmingham Conservatoire': ('Birmingham', 'GB'),
    'University of Cincinatti': ('Cincinnati', 'US'),
    'Sundin Music Hall': ('Saint Paul', 'US'),
    'St John\'s Smith Square': ('London', 'GB'),
    'Stamford Arts Centre': ('Stamford', 'GB'),
    'Sydney Opera House': ('Sydney', 'AU'),
    'The Old Court House': ('Antrim', 'GB'),
    'Theatre de Limoges': ('Limoges', 'FR'),
    'Tokyo Opera City': ('Tokyo', 'JP'),
    'Wigmore Hall': ('London', 'GB'),
    'Walt Disney Concert Hall': ('Los Angeles', 'US'),
    'Worcester Cathedral': ('Worcester', 'GB'),
    'Magdalen Farm': ('Winsham', 'GB'),
}
STREAM_ONLY_RE = re.compile(r'livestream|live youtube|digitally streamed|radio 3', re.I)


def clean_text(value):
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value or '')
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def find_city_country(location):
    folded = location.casefold()
    matches = [
        (city, code) for city, code in CITY_COUNTRIES.items()
        if re.search(rf'(?<!\w){re.escape(city.casefold())}(?!\w)', folded)
    ]
    if matches:
        city, code = max(matches, key=lambda item: len(item[0]))
        return city, code

    for marker, (city, code) in VENUE_INFERENCES.items():
        if marker.casefold() in folded:
            return city, code
    return None, None


def parse_location(location):
    city, country_code = find_city_country(location)
    if not city:
        return None

    explicit_country = None
    for name, code in sorted(COUNTRY_NAMES.items(), key=lambda item: -len(item[0])):
        if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', location, re.I):
            explicit_country = code
            break
    if explicit_country and explicit_country != country_code:
        return None

    venue = clean_text(location.split(',', 1)[0])
    venue = re.sub(r'\s*[-–—]\s*(?:Aldeburgh|George Enescu|Prague Spring).*$','', venue, flags=re.I)
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def parse_performance(node, feed_url):
    lines = [clean_text(line) for line in node.stripped_strings if clean_text(line)]
    if len(lines) < 2 or STREAM_ONLY_RE.search(' '.join(lines)):
        return None

    match = DATE_TITLE_RE.match(lines[0])
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group(1).replace(',', ''), '%d %B %Y').date().isoformat()
    except ValueError:
        return None

    title = clean_text(match.group(2))
    location = lines[-1]
    parsed_location = parse_location(location)
    link = node.find('a', href=True)
    if not title or not parsed_location or not link:
        return None
    venue, city, country_code = parsed_location
    description_parts = lines[1:-1]
    description = '\n\n'.join(description_parts) or None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(feed_url, link['href']),
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ColinMatthewsNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='colinmatthews_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for feed_url in FEED_URLS:
            try:
                response = session.get(feed_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Colin Matthews performance feed',
                    event='crawler_feed_failed', level='warning', url=feed_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            soup = BeautifulSoup(response.content, 'html.parser')
            for node in soup.select('main .entry-content p'):
                record = parse_performance(node, feed_url)
                if record:
                    records.append(record)
        return sorted(records, key=lambda row: (row['date'], row['title'], row['venue']))


def main():
    ColinMatthewsNetCrawler().run()


if __name__ == '__main__':
    main()
