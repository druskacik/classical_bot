import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://harztheater.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'spielplan/')
SOURCE = 'Harztheater'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9',
}

# The listing supplies the venue but not a separate city. Its location filter
# and venue names establish these locations, including touring performances.
VENUE_CITIES = {
    'Hans-Auenmüller-Platz (Theatervorplatz)': 'Halberstadt',
    'Kloster Huysburg': 'Huy',
    'Burg Warberg': 'Warberg',
    'Wasserschloss Westerburg': 'Huy',
    'Concordiasee': 'Seeland OT Schadeleben',
    'Börnecke': 'Blankenburg',
    'Theaterbar Café Franz': 'Halberstadt',
    'Gleimhaus': 'Halberstadt',
    'Theater Eisleben': 'Lutherstadt Eisleben',
}
CITY_MARKERS = (
    'Lutherstadt Eisleben', 'Bad Nenndorf', 'Bad Arolsen', 'Bad Elster',
    'Wolfenbüttel', 'Schöppenstedt', 'Wittenberge', 'Braunschweig',
    'Aschersleben', 'Degenershausen', 'Quedlinburg', 'Halberstadt',
    'Wernigerode', 'Blankenburg', 'Langenstein', 'Salzwedel', 'Bernburg',
    'Staßfurt', 'Stendal', 'Güstrow', 'Itzehoe', 'Uelzen', 'Stade',
    'Iserlohn', 'Rheine', 'Amberg', 'Thale', 'Zilly',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\xad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_for_venue(venue):
    if venue in VENUE_CITIES:
        return VENUE_CITIES[venue]
    folded = venue.casefold()
    for city in CITY_MARKERS:
        if city.casefold() in folded:
            return 'Stadt Osterwieck OT Zilly' if city == 'Zilly' else city
    return None


def parse_item(item):
    title = clean_text(item.select_one('h2'))
    link = item.select_one('a[href*="/veranstaltungen/"]')
    date_input = item.select_one('input[name="event-date"]')
    venue_input = item.select_one('input[name="event-ort"]')
    if not title or not link or not date_input or not venue_input:
        return None
    match = re.fullmatch(r'(\d{2}\.\d{2}\.\d{4})\s*/\s*(\d{2}:\d{2})\s*Uhr', date_input.get('value', ''))
    venue = clean_text(venue_input.get('value'))
    city = city_for_venue(venue)
    if not match or not venue or not city:
        return None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, link.get('href')),
        'time_from': match.group(2),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return clean_text(soup.select_one('.entry-content')) or None


class HarztheaterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='harztheater_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CALENDAR_URL, timeout=90)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for item in soup.select('.filter-item'):
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped Harztheater item without a valid date, venue, or city',
                    event='crawler_item_skipped', level='warning', url=CALENDAR_URL,
                    error_type='IncompleteEventData',
                    error_message='Could not extract required listing fields',
                )

        descriptions = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(detail_description, session, url): url
                for url in {record['url'] for record in records}
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Harztheater event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    descriptions[url] = None
        for record in records:
            record['description'] = descriptions.get(record['url'])
        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['city'], record['title']
        ))


def main():
    HarztheaterDeCrawler().run()


if __name__ == '__main__':
    main()
