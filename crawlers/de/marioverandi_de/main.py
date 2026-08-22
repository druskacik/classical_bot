import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.marioverandi.de/'
SOURCE = 'Mario Verandi'
NEWS_URL = urljoin(SOURCE_URL, 'news/')
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/pages?slug=news')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en,de;q=0.9',
}

# Mario Verandi is based in Berlin but the news page also lists touring events.
# Only locations explicitly present in a row are accepted.
LOCATIONS = {
    'lüdersen': ('Lüdersen', 'DE'),
    'berlin': ('Berlin', 'DE'),
    'munich': ('Munich', 'DE'),
    'münchen': ('Munich', 'DE'),
    'augsburg': ('Augsburg', 'DE'),
    'karlsruhe': ('Karlsruhe', 'DE'),
    'halle': ('Halle (Saale)', 'DE'),
    'stuttgart': ('Stuttgart', 'DE'),
    'donaueschingen': ('Donaueschingen', 'DE'),
    'backnang': ('Backnang', 'DE'),
    'vienna': ('Vienna', 'AT'),
    'wien': ('Vienna', 'AT'),
    'linz': ('Linz', 'AT'),
    'graz': ('Graz', 'AT'),
    'belfast': ('Belfast', 'GB'),
    'london': ('London', 'GB'),
    'british library': ('London', 'GB'),
    'reims': ('Reims', 'FR'),
    'paris': ('Paris', 'FR'),
    'rome': ('Rome', 'IT'),
    'madrid': ('Madrid', 'ES'),
    'barcelona': ('Barcelona', 'ES'),
    'santa fé': ('Santa Fe', 'AR'),
    'santa fe': ('Santa Fe', 'AR'),
    'rosario': ('Rosario', 'AR'),
    'sao pablo': ('São Paulo', 'BR'),
    'são paulo': ('São Paulo', 'BR'),
    'beijing': ('Beijing', 'CN'),
}

VENUES = {
    'mahalla berlin': 'Mahalla Berlin',
    'st. marien kirche': 'St. Marien Kirche',
    'after berlin': 'After Berlin',
    'morphine raum': 'Morphine Raum',
    'dim things': 'Dim Things',
    'cordillera berlin': 'Cordillera Berlin',
    'experimental music festival': 'Experimental Music Festival',
    'kulturhaus kresslesmuehle': 'Kulturhaus Kresslesmuehle',
    'kulturhaus kresslesmühle': 'Kulturhaus Kresslesmuehle',
    'andenbuch': 'Andenbuch',
    'nüüd gallery': 'nüüd gallery',
    'nüd gallery': 'nüd gallery',
    'alte schmiede': 'Alte Schmiede',
    'ausland-berlin': 'ausland',
    'nikodemus kirche': 'Nikodemus Kirche',
    'anton brückner universität': 'Anton Bruckner Privatuniversität',
    'institut für elektronische musik': 'Institut für Elektronische Musik und Akustik',
    'fourier festival': 'Fourier Festival',
    'sonido presente': 'Sonido Presente Festival',
    'spektrum': 'Spektrum',
    'sonorities festival': 'Sonorities Festival',
    'academy of the arts': 'Academy of Arts',
    'st. elisabeth kirche': 'St. Elisabeth Kirche',
    'st. georgen-kirche': 'St. Georgen-Kirche',
    'césaré': 'Césaré',
    'haus der kulturen der welt': 'Haus der Kulturen der Welt',
    'stadtbibliothek stuttgart': 'Stadtbibliothek Stuttgart',
    'villa elisabeth': 'Villa Elisabeth',
    'instituto cervantes': 'Instituto Cervantes',
    'berlin hauptbanhof': 'Berlin Hauptbahnhof',
    'berlin hauptbahnhof': 'Berlin Hauptbahnhof',
    'hauptbahnhof berlin': 'Berlin Hauptbahnhof',
    'carillon festival berlin': 'Carillon at Haus der Kulturen der Welt',
    'festival mixtur': 'Festival Mixtur',
    'zkm': 'ZKM',
    'pyramidale festival': 'pyramidale festival',
    'universidad nacional de rosario': 'Universidad Nacional de Rosario',
    'quietcue': 'quiet cue',
    'mario mazzoli gallery': 'Mario Mazzoli Gallery',
    'maison de radio france': 'Maison de Radio France',
}

EVENT_TERMS = re.compile(
    r'\b(live|concert|performance|performed|premiered|festival|listening session)\b',
    re.IGNORECASE,
)
NON_EVENT_TERMS = re.compile(
    r'\b(album|released|release|interview|broadcast|podcast|artist[- ]in[- ]residence|'
    r'installation|exhibition|talk)\b',
    re.IGNORECASE,
)


def clean_text(element):
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.match(r'^\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d{2})\b', value)
    if not match:
        # A few older entries use a hyphen before the four-digit year.
        match = re.match(r'^\s*(\d{1,2})\.(\d{1,2})-(20\d{2})\b', value)
    if not match:
        # Multi-day notices such as 24-25.5.2015 use the first performance day.
        match = re.match(r'^\s*(\d{1,2})\s*[-–]\s*\d{1,2}\.(\d{1,2})\.(20\d{2})\b', value)
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(value):
    lowered = value.casefold()
    matches = [
        (lowered.rfind(token), token, location)
        for token, location in LOCATIONS.items()
        if token in lowered
    ]
    if not matches:
        return None
    _, _, (city, country_code) = max(matches)

    venue_matches = [
        (lowered.find(token), venue)
        for token, venue in VENUES.items()
        if token in lowered
    ]
    if not venue_matches:
        return None
    _, venue = min(venue_matches)
    return venue, city, country_code


def event_url(row):
    for link in row.select('td:nth-of-type(2) a[href]'):
        href = urljoin(NEWS_URL, link.get('href', ''))
        host = urlparse(href).netloc.casefold()
        lowered = href.casefold()
        if not host or 'youtube.' in host or '/wp-content/' in lowered:
            continue
        return href
    return NEWS_URL


def make_title(value, venue):
    body = re.sub(r'^\s*\d{1,2}[.\-/]\d{1,2}[.\-/]20\d{2}\s*', '', value).strip()
    quoted = re.search(r'[“„\"]\s*([^“”„\"]{2,100}?)\s*[”\"]', body)
    if quoted:
        return quoted.group(1).strip()
    first_line = body.split('\n', 1)[0].strip(' ,“”„"')
    if re.fullmatch(r'(?:live(?: set)?(?: performance)?|sound performance)\s*(?:@|at)?', first_line, re.I):
        return f'Mario Verandi live at {venue}'
    return first_line[:180].strip()


def parse_row(row):
    cells = row.find_all('td', recursive=False)
    detail = cells[-1] if cells else row
    description = clean_text(detail)
    event_date = parse_date(description)
    if not event_date or not EVENT_TERMS.search(description):
        return None
    if NON_EVENT_TERMS.search(description):
        return None

    location = parse_location(description)
    if not location:
        return None
    venue, city, country_code = location
    title = make_title(description, venue)
    if not title:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': event_url(row),
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MarioverandiDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='marioverandi_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(API_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            pages = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Mario Verandi news API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not pages or not pages[0].get('content', {}).get('rendered'):
            raise ValueError('Mario Verandi news API returned no rendered content')

        soup = BeautifulSoup(pages[0]['content']['rendered'], 'html.parser')
        records = [record for row in soup.select('tr') if (record := parse_row(row))]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['title'], record['venue'], record['url']
            ),
        )


def main():
    MarioverandiDeCrawler().run()


if __name__ == '__main__':
    main()
