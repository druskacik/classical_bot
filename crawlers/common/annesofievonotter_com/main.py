import html
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.annesofievonotter.com/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'schedule/')
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/posts')
SOURCE = 'Anne Sofie von Otter'
CATEGORY_ID = 4

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The source has no structured location field. These are conservative venue or
# presenting-organisation matches for which a single home venue/city is clear.
# Touring/festival names without a defensible venue are deliberately omitted.
LOCATION_RULES = [
    ('royal swedish opera', 'Royal Swedish Opera', 'Stockholm', 'SE'),
    ('opernhaus zürich', 'Opernhaus Zürich', 'Zürich', 'CH'),
    ('staatsoper unter den linden', 'Staatsoper Unter den Linden', 'Berlin', 'DE'),
    ('det kongelige teater', 'The Royal Danish Theatre', 'Copenhagen', 'DK'),
    ('de geerhallen', 'De Geerhallen', 'Norrköping', 'SE'),
    ('helsinki music centre', 'Helsinki Music Centre', 'Helsinki', 'FI'),
    ('konserthuset gothenburg', 'Gothenburg Concert Hall', 'Gothenburg', 'SE'),
    ('concertgebouw amsterdam', 'Concertgebouw', 'Amsterdam', 'NL'),
    ('concertgebouw, amsterdam', 'Concertgebouw', 'Amsterdam', 'NL'),
    ('symphony hall boston', 'Symphony Hall', 'Boston', 'US'),
    ('tampere hall', 'Tampere Hall', 'Tampere', 'FI'),
    ('tokyo opera city', 'Tokyo Opera City', 'Tokyo', 'JP'),
    ('hong kong city hall', 'Hong Kong City Hall', 'Hong Kong', 'HK'),
    ('stockholm konserthuset', 'Stockholm Concert Hall', 'Stockholm', 'SE'),
    ('wigmore hall', 'Wigmore Hall', 'London', 'GB'),
    ('berwaldhallen', 'Berwaldhallen', 'Stockholm', 'SE'),
    ('dr koncerthuset', 'DR Koncerthuset', 'Copenhagen', 'DK'),
    ('la monnaie', 'La Monnaie / De Munt', 'Brussels', 'BE'),
    ('theater basel', 'Theater Basel', 'Basel', 'CH'),
    ('bayerische staatsoper', 'Bayerische Staatsoper', 'Munich', 'DE'),
    ('komische oper berlin', 'Komische Oper Berlin', 'Berlin', 'DE'),
    ('staatsoper hamburg', 'Staatsoper Hamburg', 'Hamburg', 'DE'),
    ('finnish national opera', 'Finnish National Opera', 'Helsinki', 'FI'),
    ('teatro real', 'Teatro Real', 'Madrid', 'ES'),
    ('opéra national de paris', 'Opéra Bastille', 'Paris', 'FR'),
    ('opéra national de paris, bastille', 'Opéra Bastille', 'Paris', 'FR'),
    ('théâtre des champs', 'Théâtre des Champs-Élysées', 'Paris', 'FR'),
    ('carnegie hall', 'Carnegie Hall', 'New York', 'US'),
    ('zankel hall', 'Zankel Hall at Carnegie Hall', 'New York', 'US'),
    ('barbican hall', 'Barbican Hall', 'London', 'GB'),
    ('walt disney concert hall', 'Walt Disney Concert Hall', 'Los Angeles', 'US'),
    ('benaroya hall', 'Benaroya Hall', 'Seattle', 'US'),
    ('alice tully hall', 'Alice Tully Hall', 'New York', 'US'),
    ('herbst theatre', 'Herbst Theatre', 'San Francisco', 'US'),
    ('bing concert hall', 'Bing Concert Hall', 'Stanford', 'US'),
    ('tonhalle zürich', 'Tonhalle Zürich', 'Zürich', 'CH'),
    ('tonhalle, zurich', 'Tonhalle Zürich', 'Zürich', 'CH'),
    ('konzerthaus wien', 'Konzerthaus Wien', 'Vienna', 'AT'),
    ('theater an der wien', 'Theater an der Wien', 'Vienna', 'AT'),
    ('wiener staatsoper', 'Wiener Staatsoper', 'Vienna', 'AT'),
    ('berliner philharmonie', 'Berliner Philharmonie', 'Berlin', 'DE'),
    ('teatro la fenice', 'Teatro La Fenice', 'Venice', 'IT'),
    ('teatro massimo di palermo', 'Teatro Massimo', 'Palermo', 'IT'),
    ('palau de la música catalana', 'Palau de la Música Catalana', 'Barcelona', 'ES'),
    ('philharmonie luxembourg', 'Philharmonie Luxembourg', 'Luxembourg', 'LU'),
    ('de doelen', 'De Doelen', 'Rotterdam', 'NL'),
    ('tivolivredenburg', 'TivoliVredenburg', 'Utrecht', 'NL'),
    ('muziekgebouw aan ‘t ij', 'Muziekgebouw aan ’t IJ', 'Amsterdam', 'NL'),
    ("muziekgebouw aan't ij", 'Muziekgebouw aan ’t IJ', 'Amsterdam', 'NL'),
    ('malmö opera', 'Malmö Opera', 'Malmö', 'SE'),
    ('malmö live konserthus', 'Malmö Live Konserthus', 'Malmö', 'SE'),
    ('västerås konserthus', 'Västerås Konserthus', 'Västerås', 'SE'),
    ('gävle symphony orchestra', 'Gävle Concert Hall', 'Gävle', 'SE'),
    ('gävle symfoniorkester', 'Gävle Concert Hall', 'Gävle', 'SE'),
    ('royal opera house', 'Royal Opera House', 'London', 'GB'),
    ('deutsche oper', 'Deutsche Oper Berlin', 'Berlin', 'DE'),
    ('lyric opera of chicago', 'Lyric Opera of Chicago', 'Chicago', 'US'),
]


def clean_html(value):
    soup = BeautifulSoup(html.unescape(value or ''), 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date().isoformat()
    except (TypeError, ValueError):
        return None


def infer_location(title):
    folded = title.casefold()
    for needle, venue, city, country_code in LOCATION_RULES:
        if needle.casefold() in folded:
            return venue, city, country_code
    return None


def make_record(title, event_date, url, description):
    title = clean_html(title)
    location = infer_location(title)
    if not title or not event_date or not url or location is None:
        return None
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_html(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AnneSofieVonOtterComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='annesofievonotter_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        try:
            page = 1
            total_pages = 1
            while page <= total_pages:
                response = session.get(
                    API_URL,
                    params={
                        'categories': CATEGORY_ID,
                        'per_page': 100,
                        'page': page,
                        '_fields': 'date,link,title,content,categories',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
                for post in response.json():
                    if CATEGORY_ID not in post.get('categories', []):
                        continue
                    record = make_record(
                        post.get('title', {}).get('rendered'),
                        parse_date(post.get('date')),
                        post.get('link'),
                        post.get('content', {}).get('rendered'),
                    )
                    if record:
                        records.append(record)
                page += 1

            response = session.get(SCHEDULE_URL, timeout=45)
            response.raise_for_status()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Anne Sofie von Otter schedule',
                event='crawler_fetch_failed',
                level='error',
                url=SCHEDULE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        for item in soup.select('.schedule-list'):
            date_text = clean_html(str(item.select_one('.schedule-date') or ''))
            try:
                event_date = datetime.strptime(date_text, '%d %b %Y').date().isoformat()
            except ValueError:
                continue
            title = clean_html(str(item.select_one('.schedule-title') or ''))
            description = clean_html(str(item.select_one('.schedule-feed') or ''))
            record = make_record(title, event_date, SCHEDULE_URL, description)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (record['date'], record['title'], record['venue'], record['url']),
        )


def main():
    AnneSofieVonOtterComCrawler().run()


if __name__ == '__main__':
    main()
