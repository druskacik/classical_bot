import json
import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musikfestspiele.com/de/'
PROGRAM_URL = 'https://www.musikfestspiele.com/de/programm/veranstaltungen'
SOURCE = 'Dresdner Musikfestspiele'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    elif '<' not in str(value):
        text = html.unescape(str(value))
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_json(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, ValueError):
            continue
        if isinstance(payload, list):
            events.extend(
                item for item in payload
                if isinstance(item, dict) and item.get('@type') == 'Event'
            )
        elif payload.get('@type') == 'Event':
            events.append(payload)
        elif payload.get('@type') == 'ItemList':
            events.extend(
                item for item in payload.get('itemListElement') or []
                if isinstance(item, dict) and item.get('@type') == 'Event'
            )
    return events


def parse_start(value):
    match = re.match(r'^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})', value or '')
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None
    return event_date, f'{match.group(2)}:{match.group(3)}'


def parse_location(event):
    location = event.get('location') or {}
    if isinstance(location, list):
        location = next((item for item in location if isinstance(item, dict)), {})
    if not isinstance(location, dict):
        return None

    address = location.get('address') or {}
    if not isinstance(address, dict):
        return None
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country = address.get('addressCountry') or 'DE'
    if isinstance(country, dict):
        country = country.get('name') or country.get('@id') or ''
    country = clean_text(country).upper()
    if len(country) != 2:
        country = 'DE' if country in ('DEUTSCHLAND', 'GERMANY') else ''
    if not venue or not city or not country:
        return None
    return venue, city, country


def listing_locations(soup):
    locations = {}
    for section in soup.select('.tx_events_results_section'):
        link = section.select_one('a[href*="/detail/"]')
        place = section.select_one(
            '.tx_events_results_text_information strong, '
            '.tx_events_results_item_location strong'
        )
        if not link or not place:
            continue
        lines = [clean_text(line) for line in place.stripped_strings]
        lines = [line for line in lines if line]
        combined = ' '.join(lines).lower()
        if 'kkl luzern' in combined:
            location = ('KKL Luzern (Konzertsaal)', 'Luzern', 'CH')
        elif 'champs-élysées' in combined and 'paris' in combined:
            location = ('Théâtre des Champs-Élysées', 'Paris', 'FR')
        elif 'carmen würth forum' in combined and 'künzelsau' in combined:
            location = ('Carmen Würth Forum', 'Künzelsau', 'DE')
        elif 'wiener konzerthaus' in combined:
            room = next((line for line in lines if 'saal' in line.lower()), '')
            venue = 'Wiener Konzerthaus' + (f' ({room})' if room else '')
            location = (venue, 'Wien', 'AT')
        else:
            continue
        locations[urljoin(PROGRAM_URL, link['href'])] = location
    return locations


def detail_description(soup, event):
    body = clean_text(soup.select_one('.tx_events_show_bodytext'))
    return body or clean_text(event.get('description')) or None


def make_record(event, soup=None):
    title = clean_text(event.get('name'))
    url = clean_text(event.get('url'))
    start = parse_start(event.get('startDate'))
    location = parse_location(event)
    if not title or not url or not start or not location:
        return None
    event_date, time_from = start
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': detail_description(soup, event) if soup else clean_text(
            event.get('description')
        ) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(session, event):
    url = clean_text(event.get('url'))
    if not url:
        return None
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    details = event_json(soup)
    detail = details[0] if details else event
    detail.setdefault('url', url)
    if event.get('location'):
        detail.setdefault('location', event['location'])
    return make_record(detail, soup)


class MusikfestspieleComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musikfestspiele_com',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(PROGRAM_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Dresdner Musikfestspiele programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAM_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        programme_soup = BeautifulSoup(response.text, 'html.parser')
        events = event_json(programme_soup)
        locations = listing_locations(programme_soup)
        for item in events:
            if not parse_location(item) and item.get('url') in locations:
                venue, city, country = locations[item['url']]
                item['location'] = {
                    '@type': 'Place',
                    'name': venue,
                    'address': {
                        '@type': 'PostalAddress',
                        'addressLocality': city,
                        'addressCountry': country,
                    },
                }
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_detail, session, item): item for item in events}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Dresdner Musikfestspiele event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=clean_text(item.get('url')),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    record = make_record(item)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MusikfestspieleComCrawler().run()


if __name__ == '__main__':
    main()
