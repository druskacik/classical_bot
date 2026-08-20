import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bomsorikim.com/'
TOUR_URL = f'{SOURCE_URL}tour/'
SOURCE = 'Bomsori Kim'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# Many entries omit the country because the city itself is unambiguous.  These
# mappings only normalize locations explicitly printed on the tour page.
CITY_COUNTRIES = {
    'amsterdam': ('Amsterdam', 'NL'),
    'basel': ('Basel', 'CH'),
    'berlin': ('Berlin', 'DE'),
    'bilbao': ('Bilbao', 'ES'),
    'cagliari': ('Cagliari', 'IT'),
    'cologne': ('Cologne', 'DE'),
    'copenhagen': ('Copenhagen', 'DK'),
    'düsseldorf': ('Düsseldorf', 'DE'),
    'ferrara': ('Ferrara', 'IT'),
    'helsinki': ('Helsinki', 'FI'),
    'izmir': ('Izmir', 'TR'),
    'los angeles': ('Los Angeles', 'US'),
    'madrid': ('Madrid', 'ES'),
    'manchester': ('Manchester', 'GB'),
    'pamplona': ('Pamplona', 'ES'),
    'paris': ('Paris', 'FR'),
    'perugia': ('Perugia', 'IT'),
    'san sebastian': ('San Sebastián', 'ES'),
    'san sebastián': ('San Sebastián', 'ES'),
    'saratoga springs': ('Saratoga Springs', 'US'),
    'tafalla': ('Tafalla', 'ES'),
    'the hague': ('The Hague', 'NL'),
    'turin': ('Turin', 'IT'),
    'vienna': ('Vienna', 'AT'),
    'vilnius': ('Vilnius', 'LT'),
    'vitoria-gasteiz': ('Vitoria-Gasteiz', 'ES'),
    'vitoria‑gasteiz': ('Vitoria-Gasteiz', 'ES'),
    'warsaw': ('Warsaw', 'PL'),
    'wiesbaden': ('Wiesbaden', 'DE'),
    'zürich': ('Zürich', 'CH'),
}

COUNTRIES = {
    'belgium': 'BE', 'dänmark': 'DK', 'france': 'FR', 'germany': 'DE',
    'italy': 'IT', 'finnland': 'FI', 'lithuania': 'LT',
    'netherlands': 'NL', 'poland': 'PL', 'spain': 'ES',
    'switzerland': 'CH', 'turkey': 'TR', 'uk': 'GB', 'usa': 'US',
}

DATE_RE = re.compile(
    r'^(?P<days>\d{1,2}(?:\s*&\s*\d{1,2})?)\s*'
    r'(?P<month>January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\b',
    re.I,
)

VENUE_RE = re.compile(
    r'\b(?:amare|auditori(?:o|um)|baluarte|concertgebouw|hall|konzerthaus|'
    r'kulturgunea|kulturzentrum|kurhaus|mozarteum|philharmonie|saal|sanat merkezi|'
    r'schloss|teatro|tonhalle|zentrum)\b',
    re.I,
)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip(' ,')


def split_lines(element):
    return [clean_text(value) for value in element.stripped_strings if clean_text(value)]


def collect_blocks(container):
    blocks = []
    current = None
    for paragraph in container.select('p'):
        lines = split_lines(paragraph)
        if not lines:
            continue
        match = DATE_RE.match(lines[0])
        if match:
            if current:
                blocks.append(current)
            current = {'date_text': lines.pop(0), 'lines': lines}
        elif current:
            current['lines'].extend(lines)
    if current:
        blocks.append(current)
    return blocks


def parse_dates(value, year):
    match = DATE_RE.match(value)
    if not match:
        return []
    month = MONTHS[match.group('month').lower()]
    results = []
    for day_text in re.split(r'\s*&\s*', match.group('days')):
        try:
            results.append(date(year, month, int(day_text)).isoformat())
        except ValueError:
            return []
    return results


def parse_location(lines):
    if not lines:
        return None

    first = clean_text(lines[0])
    folded = first.casefold()

    # A few listings put a well-known venue and city on the same line.
    if folded.startswith('concertgebouw, amsterdam'):
        return 'Concertgebouw', 'Amsterdam', 'NL', 1
    if folded.startswith('hollywood bowl, los angeles'):
        return 'Hollywood Bowl', 'Los Angeles', 'US', 1
    if folded.startswith('saratoga springs, spac, usa'):
        return 'SPAC', 'Saratoga Springs', 'US', 1
    if folded.startswith('salzburger festspiele'):
        return 'Stiftung Mozarteum – Großer Saal', 'Salzburg', 'AT', 2
    if folded.startswith('rheingau musik festival'):
        following = clean_text(lines[1]) if len(lines) > 1 else ''
        if following.casefold() == 'schloss johannisberg':
            return following, 'Geisenheim', 'DE', 2
        if following.casefold() == 'kurhaus wiesbaden':
            return following, 'Wiesbaden', 'DE', 2

    pieces = [clean_text(part) for part in first.split(',') if clean_text(part)]
    city_key = pieces[0].casefold() if pieces else ''
    city_info = CITY_COUNTRIES.get(city_key)
    country_code = COUNTRIES.get(pieces[-1].casefold()) if len(pieces) > 1 else None
    if city_info:
        city, inferred_country = city_info
        country_code = country_code or inferred_country
    else:
        return None

    for index, line in enumerate(lines[1:], start=1):
        if VENUE_RE.search(line):
            return clean_text(line), city, country_code, index + 1
    return None


def make_title(description_lines):
    for line in reversed(description_lines):
        if re.search(r'\b(?:concerto|recital|sonata)s?\b', line, re.I):
            return f'Bomsori Kim – {line}'
    for line in description_lines:
        if re.search(r'\b(?:orchestra|orchester|philharmonic|philharmonie)\b', line, re.I):
            return f'Bomsori Kim with {line}'
    return 'Bomsori Kim in concert'


def parse_block(block, year):
    event_dates = parse_dates(block['date_text'], year)
    location = parse_location(block['lines'])
    if not event_dates or not location:
        return []

    venue, city, country_code, _ = location
    description_lines = [line for line in block['lines'] if line.casefold() not in {'tickets', 'website'}]
    description = '\n'.join(description_lines) or None
    title = make_title(description_lines)
    return [{
        'title': title,
        'date': event_date,
        'url': TOUR_URL,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in event_dates]


class BomsoriKimComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bomsorikim_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(TOUR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Bomsori Kim tour page',
                event='crawler_fetch_failed',
                level='error',
                url=TOUR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        container = soup.select_one('#cc-m-textwithimage-15272219325')
        if container is None:
            raise ValueError('Could not find the Bomsori Kim tour schedule')

        year_element = container.find(string=re.compile(r'^\s*20\d{2}\s*$'))
        if year_element is None:
            raise ValueError('Could not find the tour schedule year')
        year = int(clean_text(year_element))

        records = []
        for block in collect_blocks(container):
            parsed = parse_block(block, year)
            if not parsed:
                log_message(
                    'Skipped Bomsori Kim event without a defensible location',
                    event='crawler_item_skipped',
                    level='warning',
                    url=TOUR_URL,
                    error_type='IncompleteEventData',
                    error_message='Required date, city, country, or venue is missing',
                )
            records.extend(parsed)

        return sorted(records, key=lambda item: (item['date'], item['venue'], item['title']))


def main():
    BomsoriKimComCrawler().run()


if __name__ == '__main__':
    main()
