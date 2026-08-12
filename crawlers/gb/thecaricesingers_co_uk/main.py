import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.thecaricesingers.co.uk/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SITEMAP_URL = urljoin(SOURCE_URL, 'event-pages-sitemap.xml')
SOURCE = 'The Carice Singers'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

CITY_MARKERS = (
    'Chipping Campden', 'Shipston-on-Stour', 'Cheltenham', 'Huddersfield',
    'Worcester', 'Bradford', 'Richmond', 'Dorking', 'Oxford', 'Utrecht', 'London',
)


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    urls = []

    sitemap = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    urls.extend(
        clean_text(node)
        for node in sitemap.select('url > loc')
        if '/event-details/' in clean_text(node)
    )

    # Wix can publish a new event before its event sitemap is refreshed.
    concerts = BeautifulSoup(get_response(session, CONCERTS_URL).content, 'html.parser')
    urls.extend(
        urljoin(SOURCE_URL, anchor['href'])
        for anchor in concerts.select('a[href*="/event-details/"]')
    )
    return list(dict.fromkeys(url.split('?', 1)[0] for url in urls))


def parse_date_and_time(value):
    match = re.search(
        r'([A-Z][a-z]{2} \d{1,2}, 20\d{2}),\s*'
        r'(\d{1,2}:\d{2} [AP]M)',
        value,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%b %d, %Y').date().isoformat()
        event_time = datetime.strptime(match.group(2), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None, None
    return event_date, event_time


def city_for(location):
    for city in CITY_MARKERS:
        if re.search(rf'\b{re.escape(city)}\b', location, re.IGNORECASE):
            return city
    return None


def venue_for(location, city):
    first = location.split(',', 1)[0].strip()
    if not first or first.casefold() == city.casefold():
        # Wix sometimes stores only a city plus street address. These two
        # recurring locations are unambiguous from the address and event copy.
        if re.search(r'\bTrafalgar (?:Sq|Square)\b', location, re.IGNORECASE):
            return 'St Martin-in-the-Fields'
        return None
    return first


def description_for(soup):
    parts = []
    for hook in ('event-description', 'about-section'):
        text = clean_text(soup.select_one(f'[data-hook="{hook}"]'))
        text = re.sub(r'^About the Event\s*', '', text, flags=re.IGNORECASE)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('[data-hook="event-title"]'))
    date_text = clean_text(soup.select_one('[data-hook="event-full-date"]'))
    location = clean_text(soup.select_one('[data-hook="event-full-location"]'))
    event_date, time_from = parse_date_and_time(date_text)
    city = city_for(location)
    venue = venue_for(location, city) if city else None
    if not all((title, event_date, location, city, venue)):
        return None

    country_code = 'NL' if re.search(r'\bNetherlands\b', location, re.IGNORECASE) else 'GB'
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_for(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result().content, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape The Carice Singers event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class TheCariceSingersCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thecaricesingers_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheCariceSingersCrawler().run()


if __name__ == '__main__':
    main()
