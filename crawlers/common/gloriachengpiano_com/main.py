import calendar
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://gloriachengpiano.com/'
PERFORMANCES_URL = urljoin(SOURCE_URL, 'gloria_cheng_performances.html')
SOURCE = 'Gloria Cheng'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

# The second column mixes halls, presenters, and festivals. These stable names
# are the only location evidence available for many rows in this touring
# artist's archive. More specific text in a row takes precedence.
LOCATION_RULES = [
    (r'Bari', 'Bari', 'IT'),
    (r'Athens|Stavros Niarchos', 'Athens', 'GR'),
    (r'Chania|Crete', 'Chania', 'GR'),
    (r'Amsterdam|Muziekgeb', 'Amsterdam', 'NL'),
    (r'Barcelona|Palau de la M', 'Barcelona', 'ES'),
    (r'Nanning|China-ASEAN', 'Nanning', 'CN'),
    (r'Beijing', 'Beijing', 'CN'),
    (r'Halifax|Atlantic Film Festival', 'Halifax', 'CA'),
    (r'Franche Comt|Du Vert', 'Salins-les-Bains', 'FR'),
    (r'Los Angeles|UCLA|Schoenberg Hall|Walt Disney|LA Phil|Green Umbrella|'
     r'Monday Evening Concerts|Piano Spheres|Angel City|Hear Now|HEAR NOW|'
     r'Hammer Museum|Minimalist Jukebox|American Youth Symphony|L\.A\. Dance|'
     r'LA Jazz|Sam First|2020 Arts|Noon-to-Midnight', 'Los Angeles', 'US'),
    (r'Santa Monica|Jacaranda|Libretto|Belmont Heights|Orchestra Santa Monica',
     'Santa Monica', 'US'),
    (r'Pasadena|Boston Court|Pittance|Harpsichord Center', 'Pasadena', 'US'),
    (r'La Canada Flintridge|Descanso Gardens', 'La Canada Flintridge', 'US'),
    (r'Paso Robles', 'Paso Robles', 'US'),
    (r'La Jolla', 'La Jolla', 'US'),
    (r'UC San Diego|UCSD', 'San Diego', 'US'),
    (r'Orange County', 'Santa Ana', 'US'),
    (r'Long Beach', 'Long Beach', 'US'),
    (r'Chico', 'Chico', 'US'),
    (r'Fresno', 'Fresno', 'US'),
    (r'Mendocino', 'Mendocino', 'US'),
    (r'Ojai', 'Ojai', 'US'),
    (r'Berkeley|Cal Performances|UC Berkeley|Ojai North', 'Berkeley', 'US'),
    (r'Oakland|Dresher Ensemble', 'Oakland', 'US'),
    (r'San Francisco|SF Jazz|Other Minds|Old First Church|Yerba Buena', 'San Francisco', 'US'),
    (r'Stanford', 'Stanford', 'US'),
    (r'Sacramento|Festival of New American Music', 'Sacramento', 'US'),
    (r'New England Philharmonic|Boston\b|Northeastern|Tanglewood', 'Boston', 'US'),
    (r'New York|NYC|Brooklyn|National Sawdust|Poisson Rouge|Bargemusic|Tenri', 'New York', 'US'),
    (r'Albany Symphony', 'Albany', 'US'),
    (r'Chautauqua', 'Chautauqua', 'US'),
    (r'Duke University', 'Durham', 'US'),
    (r'Bucknell', 'Lewisburg', 'US'),
    (r'Cornell', 'Ithaca', 'US'),
    (r'University of Illinois|Champaign', 'Urbana', 'US'),
    (r'University of Missouri|Kansas City', 'Kansas City', 'US'),
    (r'University of South Carolina', 'Columbia', 'US'),
    (r'College of Charleston', 'Charleston', 'US'),
    (r'William Paterson', 'Wayne', 'US'),
    (r'Montclair State', 'Montclair', 'US'),
    (r'Western Washington|Bellingham', 'Bellingham', 'US'),
    (r'Carleton College|Northfield', 'Northfield', 'US'),
    (r'Brown University|Providence', 'Providence', 'US'),
    (r'Honesdale', 'Honesdale', 'US'),
    (r'Breckenridge', 'Breckenridge', 'US'),
    (r'Seattle', 'Seattle', 'US'),
    (r'Baltimore', 'Baltimore', 'US'),
    (r'Arizona Friends', 'Tucson', 'US'),
]

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'june': 6, 'jul': 7, 'july': 7, 'aug': 8, 'sept': 9, 'sep': 9,
    'oct': 10, 'nov': 11, 'dec': 12,
}
LINK_LABEL_RE = re.compile(
    r'\b(?:Click for more info|Click to watch|Watch online|Read (?:the )?review(?: #\d)?|'
    r'Read the (?:feature article|Review)|Interview (?:BHMA|Athens Voice))\b', re.I
)


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    text = clean_text(value).replace(',', ' ')
    match = re.search(
        r'(?i)\b(Jan|Feb|Mar|Apr|May|Jun(?:e)?|Jul(?:y)?|Aug|Sep(?:t)?|Oct|Nov|Dec)\.?' 
        r'\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s+(\d{4})\b', text
    )
    if not match:
        return []
    month = MONTHS[match.group(1).lower()]
    first, last, year = int(match.group(2)), int(match.group(3) or match.group(2)), int(match.group(4))
    if last < first or last > calendar.monthrange(year, month)[1]:
        return []
    try:
        return [date(year, month, day).isoformat() for day in range(first, last + 1)]
    except ValueError:
        return []


def parse_time(value):
    match = re.search(r'(?i)\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', clean_text(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if match.group(3).lower() == 'pm' else 0)
    return f'{hour:02d}:{minute:02d}'


def resolve_location(venue, description):
    evidence = f'{venue}\n{description}'
    for pattern, city, country_code in LOCATION_RULES:
        if re.search(pattern, evidence, re.I):
            return city, country_code
    return None, None


def row_url(row):
    for anchor in row.select('a[href]'):
        href = anchor.get('href', '').strip()
        label = clean_text(anchor.get_text(' ', strip=True))
        if href and not re.search(r'review|interview|watch', label, re.I):
            return urljoin(PERFORMANCES_URL, href)
    return PERFORMANCES_URL


def records_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.date_table')
    if not table:
        return []
    records = []
    for row in table.select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 3:
            continue
        date_text = clean_text(cells[0])
        dates = parse_dates(date_text)
        venue = clean_text(cells[1])
        raw_description = clean_text(cells[2])
        description = LINK_LABEL_RE.sub('', raw_description).strip(' \n-')
        city, country_code = resolve_location(venue, description)
        if not dates or not venue or not description or not city or not country_code:
            continue
        title = description.split('\n', 1)[0].strip()
        for event_date in dates:
            records.append({
                'title': title,
                'date': event_date,
                'url': row_url(row),
                'time_from': parse_time(date_text),
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(PERFORMANCES_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    records = records_from_html(response.content)
    if not records:
        log_message(
            'No Gloria Cheng performance records found',
            event='crawler_empty_listing',
            level='warning',
            url=PERFORMANCES_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class GloriaChengPianoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gloriachengpiano_com',
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
        return scrape_concerts()


def main():
    GloriaChengPianoComCrawler().run()


if __name__ == '__main__':
    main()
