import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rsb-online.de/'
CALENDAR_URL = f'{SOURCE_URL}konzerte-uebersicht-tickets/'
SOURCE = 'Rundfunk-Sinfonieorchester Berlin'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The RSB calendar includes its touring concerts. These venue mappings keep
# the home-city default from being applied to explicitly advertised tours.
LOCATION_MARKERS = {
    'barclays arena hamburg': ('Hamburg', 'DE'),
    'basilika ottobeuren': ('Ottobeuren', 'DE'),
    'congress innsbruck': ('Innsbruck', 'AT'),
    'elbphilharmonie hamburg': ('Hamburg', 'DE'),
    'festival- und kongresszentrum warna': ('Warna', 'BG'),
    'heinrich-lades-halle erlangen': ('Erlangen', 'DE'),
    'isarphilharmonie münchen': ('München', 'DE'),
    'kloster chorin': ('Chorin', 'DE'),
    'konzert theater coesfeld': ('Coesfeld', 'DE'),
    'wiener konzerthaus': ('Wien', 'AT'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_location(venue):
    venue = clean_text(venue)
    if not venue:
        return None
    normalized = venue.casefold()
    for marker, (city, country_code) in LOCATION_MARKERS.items():
        if marker in normalized:
            return venue, city, country_code

    # The remaining locations in this orchestra's calendar are Berlin rooms
    # and venues. An unknown explicitly named city is not defaulted to Berlin.
    if ' berlin' in normalized or normalized.endswith('berlin'):
        return venue, 'Berlin', 'DE'
    if normalized in {
        'haus des rundfunks', 'humboldt forum', 'phil­harmonie foyer',
        'philharmonie foyer', 'radialsystem v', 'silent green',
        'theater im delphi', 'uber arena',
    }:
        return venue, 'Berlin', 'DE'
    return None


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def detail_description(soup):
    content = soup.select_one('.ConcertContent-Main')
    description = clean_text(content)
    return description or None


def listing_occurrences(soup):
    occurrences = []
    current_year = date.today().year
    previous_month = None

    for item in soup.select('.ConcertListItem'):
        link = item.select_one('a.ConcertListItem-Content[href]')
        title = clean_text(item.select_one('.ConcertListItem-Title'))
        if not link or not title:
            continue
        times = item.select('.ConcertListItem-Details time')
        places = item.select('.ConcertListItem-Details .ConcertListItem-Place')
        for time_element, place_element in zip(times, places):
            match = re.search(r'(\d{1,2})\.(\d{1,2})\.', clean_text(time_element))
            place_text = clean_text(place_element)
            time_match = re.match(r'(\d{1,2}):(\d{2})\s+(.+)', place_text)
            if not match or not time_match:
                continue
            day, month = int(match.group(1)), int(match.group(2))
            if previous_month is not None and month < previous_month:
                current_year += 1
            previous_month = month
            try:
                event_date = date(current_year, month, day).isoformat()
            except ValueError:
                continue
            location = resolve_location(time_match.group(3))
            if not location:
                continue
            venue, city, country_code = location
            occurrences.append({
                'title': title,
                'date': event_date,
                'url': link['href'],
                'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}',
                'venue': venue,
                'city': city,
                'country_code': country_code,
            })
    return occurrences


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    occurrences = listing_occurrences(fetch_soup(session, CALENDAR_URL))
    descriptions = {}

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(fetch_soup, session, url): url
            for url in {record['url'] for record in occurrences}
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = detail_description(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                descriptions[url] = None

    for record in occurrences:
        record.update({
            'description': descriptions.get(record['url']),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return sorted(
        occurrences,
        key=lambda record: (record['date'], record['time_from'], record['title'], record['url']),
    )


class RsbOnlineDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rsb_online_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    RsbOnlineDeCrawler().run()


if __name__ == '__main__':
    main()
