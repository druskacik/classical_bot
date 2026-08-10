import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from threading import Lock
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.haendel-festspiele.de/'
SOURCE = 'Internationale Händel-Festspiele Göttingen'
PROGRAM_URL = urljoin(SOURCE_URL, '/de/programm/')
ARCHIVE_URL = urljoin(PROGRAM_URL, 'rueckblick/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'märz': 3,
    'april': 4,
    'mai': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'dezember': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s+(20\d{2})', value)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_listing_event(element, listing_url):
    title_link = element.select_one('a.event-link-ht[href]')
    location = clean_text(element.select_one('.location'))
    parts = [part.strip() for part in location.split('|')]
    if title_link is None or len(parts) < 3:
        return None

    title = clean_text(title_link)
    event_date = parse_date(parts[1])
    time_match = re.search(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b', parts[0])
    venue = clean_text(parts[-1])
    if not title or not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': urljoin(listing_url, title_link['href']),
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
    }


def venue_city(session, venue_url):
    response = session.get(venue_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    main_text = clean_text(soup.select_one('main'))
    match = re.search(r'\b\d{5}\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß .()/-]+)', main_text)
    if not match:
        return None
    city = match.group(1).split('\n', 1)[0].strip(' ,|')
    return city or None


def detail_description(soup):
    parts = []
    for selector in ('.event-uprow', '.event-lowrow-text2', '.event-lowrow-artists'):
        value = clean_text(soup.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def enrich_event(session, event, city_cache, cache_lock):
    response = session.get(event['url'], timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    venue_link = soup.select_one('.event-dateloc-loc a[href*="/spielstaetten/"]')
    if venue_link is None:
        # A few touring venues have no venue profile, but name their city
        # explicitly after a comma (for example "Welfenschloss, Hann. Münden").
        if ',' not in event['venue']:
            return None
        city = event['venue'].rsplit(',', 1)[1].strip()
        if not city or any(character.isdigit() for character in city):
            return None
        return {
            **event,
            'city': city,
            'country_code': 'DE',
            'description': detail_description(soup),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
    venue_url = urljoin(event['url'], venue_link['href'])
    with cache_lock:
        cached = city_cache.get(venue_url)
    if cached is None:
        city = venue_city(session, venue_url)
        if not city:
            return None
        with cache_lock:
            city_cache[venue_url] = city
    else:
        city = cached

    return {
        **event,
        'city': city,
        'country_code': 'DE',
        'description': detail_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HaendelFestspieleDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='haendel_festspiele_de',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            archive_response = session.get(ARCHIVE_URL, timeout=45)
            archive_response.raise_for_status()
            archive_soup = BeautifulSoup(archive_response.text, 'html.parser')
            year_urls = {
                urljoin(ARCHIVE_URL, link['href'])
                for link in archive_soup.select('a[href*="/de/programm/20"]')
                if re.search(r'/de/programm/20\d{2}/?$', link.get('href', ''))
            }
            year_urls.add(urljoin(PROGRAM_URL, f'{date.today().year}/'))

            events = []
            for year_url in sorted(year_urls):
                response = session.get(year_url, timeout=45)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                # Programme years are split into date ranges. The selected
                # range depends on today's date, while links with `t=` expose
                # every other range, including already elapsed concerts.
                range_urls = {year_url}
                range_urls.update(
                    urljoin(year_url, link['href'])
                    for link in soup.select('a[href*="?t="]')
                    if re.search(r'[?&]t=20\d{2}-\d{2}-\d{2}', link.get('href', ''))
                )
                for range_url in sorted(range_urls):
                    range_response = response if range_url == year_url else session.get(
                        range_url, timeout=45
                    )
                    range_response.raise_for_status()
                    range_soup = BeautifulSoup(range_response.text, 'html.parser')
                    events.extend(
                        event for element in range_soup.select('.prog-event')
                        if (event := parse_listing_event(element, range_url))
                    )

            events = list({
                (event['url'], event['date'], event['time_from']): event for event in events
            }.values())
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Händel-Festspiele programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAM_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        city_cache = {}
        cache_lock = Lock()
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(enrich_event, session, event, city_cache, cache_lock): event
                for event in events
            }
            for future in as_completed(futures):
                event = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Händel-Festspiele event',
                        event='crawler_item_failed',
                        level='warning',
                        url=event['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    HaendelFestspieleDeCrawler().run()


if __name__ == '__main__':
    main()
