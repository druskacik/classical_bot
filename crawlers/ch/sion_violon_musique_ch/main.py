import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sion-violon-musique.ch/'
EVENTS_URL = urljoin(SOURCE_URL, 'tous-les-evenements-et-concerts/')
SOURCE = 'Sion Violon Musique'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CH,fr;q=0.9,de;q=0.7,en;q=0.5',
}

# The catalogue also advertises long-running courses, exhibitions, and a
# festival overview. These are not individual performances.
NON_CONCERT_TITLES = (
    'exposition ',
    'line-up',
    'masterclass',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u2008', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = re.search(r'\b(\d{2}\.\d{2}\.\d{2})\b', value)
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%y').date().isoformat()
    except ValueError:
        return None, None

    time_match = re.search(r'[>›]\s*([01]?\d|2[0-3])[:h]([0-5]\d)\b', value)
    event_time = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, event_time


def parse_location(soup):
    location = soup.select_one('.event-map .map-adresse')
    if location is None:
        return None

    venue = clean_text(location.select_one('.prof-exer'))
    address = clean_text(location)
    city_match = re.search(r'\b\d{4}\s+([^\n,]+)', address)
    if not venue or not city_match:
        return None

    city = city_match.group(1).strip()
    # Country names sometimes follow the municipality on the same text line.
    city = re.split(r'\s+(?:Suisse|Switzerland|Schweiz)\b', city, maxsplit=1)[0].strip()
    if not city or venue.casefold() == city.casefold():
        return None
    return venue, city


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('.intro-titre h1.prof-titre')
    date_element = soup.select_one('.intro-titre .obj-date')
    title = clean_text(heading)
    date_text = clean_text(date_element)
    if title and date_text and title.endswith(date_text):
        title = title[:-len(date_text)].strip()

    if not title or title.casefold().startswith(NON_CONCERT_TITLES):
        return None

    event_date, event_time = parse_date_time(date_text)
    location = parse_location(soup)
    if not event_date or not location:
        return None

    venue, city = location
    description = clean_text(soup.select_one('.prof-pres')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'CH',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return parse_event(response.text, url)


class SionViolonMusiqueChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sion_violon_musique_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(EVENTS_URL, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Sion Violon Musique catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        urls = list(dict.fromkeys(
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select('a[href*="/events/"][href]')
        ))
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to process Sion Violon Musique event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    SionViolonMusiqueChCrawler().run()


if __name__ == '__main__':
    main()
