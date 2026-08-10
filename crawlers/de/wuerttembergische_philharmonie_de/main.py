import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wuerttembergische-philharmonie.de/'
CURRENT_URL = urljoin(SOURCE_URL, 'musik/alle-konzerte/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'musik/konzertarchiv/')
SOURCE = 'Württembergische Philharmonie Reutlingen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The orchestra is German, but its own calendar includes touring concerts.
FOREIGN_CITIES = {
    'Amsterdam': 'NL',
    'Basel': 'CH',
    'Belfort': 'FR',
    'Besançon': 'FR',
    'Bischofszell': 'CH',
    'Brugg': 'CH',
    'Cremona CR': 'IT',
    'Luzern': 'CH',
    'Mailand': 'IT',
    'Milano': 'IT',
    'Riorges (Roanne)': 'FR',
    'Schaffhausen': 'CH',
    'Villach': 'AT',
    'Wels': 'AT',
    'Wien': 'AT',
    'Zürich': 'CH',
}

# A handful of old PHILMO listings put the venue in Contao's city field. The
# visible location or title identifies the municipality unambiguously.
CITY_CORRECTIONS = {
    'Betzingen': 'Reutlingen',
    'Bronnweiler': 'Reutlingen',
    'Bürgerzentrum': 'Waiblingen',
    'FILUM': 'Filderstadt',
    'Gastro-Bereich': 'Reutlingen',
    'Gönningen': 'Reutlingen',
    'Habila Rappertshofen': 'Reutlingen',
    'Haus der Familie': 'Reutlingen',
    'Hohbuch': 'Reutlingen',
    'Innoport': 'Reutlingen',
    'Münster St. Maria und Markus': 'Reichenau',
    'Ohmenhausen': 'Reutlingen',
    'St. Wolfgangschule': 'Reutlingen',
    'Stadtbibliothek Reutlingen': 'Reutlingen',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def archive_page_count(soup):
    pages = [1]
    for link in soup.select('a[href*="page_e41="]'):
        values = parse_qs(urlparse(link.get('href', '')).query).get('page_e41', [])
        if values and values[0].isdigit():
            pages.append(int(values[0]))
    return max(pages)


def listing_items(soup):
    items = []
    for link in soup.select('.mod_eventlist .event a.link-container[href*="/konzert/"]'):
        event = link.find_parent(class_='event')
        time_node = link.select_one('time[datetime]')
        title_node = link.select_one('.title')
        location_node = link.select_one('.location')
        if not event or not time_node or not title_node or not location_node:
            continue
        url = urljoin(SOURCE_URL, link.get('href', ''))
        city = clean_text(event.get('data-ort'))
        location = clean_text(location_node.get_text(' ', strip=True))
        if not city:
            # Most records use "City, Venue". Ambiguous venue-only strings are
            # intentionally left without a city and skipped later.
            city = clean_text(location.split(',', 1)[0]) if ',' in location else ''
        city = CITY_CORRECTIONS.get(city, city)
        items.append(
            {
                'url': url,
                'title': clean_text(title_node.get_text(' ', strip=True)),
                'starts_at': time_node.get('datetime', ''),
                'city': city,
                'location': location,
            }
        )
    return items


def discover_events(session):
    current = BeautifulSoup(get_response(session, CURRENT_URL).text, 'html.parser')
    first_archive = BeautifulSoup(get_response(session, ARCHIVE_URL).text, 'html.parser')
    items = listing_items(current) + listing_items(first_archive)

    page_count = archive_page_count(first_archive)
    for page in range(2, page_count + 1):
        soup = BeautifulSoup(
            get_response(session, ARCHIVE_URL, params={'page_e41': page}).text,
            'html.parser',
        )
        items.extend(listing_items(soup))

    # An event can be linked from multiple category lists on the same page.
    return list({item['url']: item for item in items}.values())


def venue_from_location(location, city):
    location = clean_text(location)
    city = clean_text(city)
    if not location or not city:
        return ''
    parts = [part.strip() for part in location.split(',') if part.strip()]
    if len(parts) > 1 and parts[0].casefold() == city.casefold():
        return ', '.join(parts[1:])
    if len(parts) > 1 and parts[-1].casefold() == city.casefold():
        return ', '.join(parts[:-1])
    # data-ort supplies the city separately, so a non-city location is a venue.
    if location.casefold() != city.casefold():
        return location
    return ''


def detail_description(soup):
    event = soup.select_one('.mod_eventreader .event')
    if not event:
        return None
    parts = []
    for selector in ('.event-text .teaser', '.event-text .text .text.rte'):
        node = event.select_one(selector)
        text = clean_text(node.get_text('\n', strip=True) if node else '')
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def make_record(item, response_text):
    soup = BeautifulSoup(response_text, 'html.parser')
    reader = soup.select_one('.mod_eventreader .event')
    if not reader:
        return None

    title_node = reader.select_one('h1.title')
    time_node = reader.select_one('time[datetime]')
    location_node = reader.select_one('.info .location')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else item['title'])
    starts_at = time_node.get('datetime', '') if time_node else item['starts_at']
    city = item['city']
    location = clean_text(
        location_node.get_text(' ', strip=True) if location_node else item['location']
    )
    venue = venue_from_location(location, city)
    if not title or not starts_at or not city or not venue or not item['url']:
        return None
    try:
        parsed = datetime.fromisoformat(starts_at)
    except (TypeError, ValueError):
        return None

    has_time = 'T' in starts_at
    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': item['url'],
        'time_from': parsed.strftime('%H:%M') if has_time else None,
        'venue': venue,
        'city': city,
        'country_code': FOREIGN_CITIES.get(city, 'DE'),
        'description': detail_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = discover_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_response, session, item['url']): item for item in items
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = make_record(item, future.result().text)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape WPR concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['url'],
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


class WuerttembergischePhilharmonieDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wuerttembergische_philharmonie_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    WuerttembergischePhilharmonieDeCrawler().run()


if __name__ == '__main__':
    main()
