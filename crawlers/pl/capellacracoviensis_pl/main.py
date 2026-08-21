import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://capellacracoviensis.pl/'
SOURCE = 'Capella Cracoviensis'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/angio_events'
DEFAULT_CITY = 'Kraków'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

COUNTRY_MARKERS = {
    'Austria': 'AT', 'Belgia': 'BE', 'Belgium': 'BE', 'Czechy': 'CZ',
    'Czech Republic': 'CZ', 'Francja': 'FR', 'France': 'FR', 'Germany': 'DE',
    'Niemcy': 'DE', 'Hiszpania': 'ES', 'Italy': 'IT', 'Włochy': 'IT',
    'Litwa': 'LT', 'Netherlands': 'NL', 'Holandia': 'NL', 'Norwegia': 'NO',
    'Polska': 'PL', 'Poland': 'PL', 'Słowacja': 'SK', 'Switzerland': 'CH',
    'Szwajcaria': 'CH', 'United Kingdom': 'GB', 'Wielka Brytania': 'GB',
}

CITY_COUNTRIES = {
    # The source occasionally publishes its own foreign tour performances.
    # These cities are explicit in the event copy, rather than inferred from
    # Capella Cracoviensis's Kraków base.
    'Schwarzenberg': 'DE',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', value)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(3)), int(match.group(2)), int(match.group(1))
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def detail_values(soup):
    values = {}
    for item in soup.select('.details-list li'):
        name = clean_text(item.select_one('.details-list__name')).lower()
        value = clean_text(item.select_one('.details-list__data'))
        if name and value:
            values[name] = value
    return values


def body_location(description):
    # Event copy normally starts with "time City | venue" for tour dates.  A
    # local event generally omits the city, in which case Kraków is defensible.
    for line in description.splitlines()[:6]:
        match = re.search(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\s+(.+)', line)
        if not match:
            continue
        location = match.group(1).strip()
        if '|' in location:
            city, venue = (part.strip(' ,') for part in location.split('|', 1))
            if city and venue:
                return city, venue
    return None, None


def country_code(text, city):
    if city in CITY_COUNTRIES:
        return CITY_COUNTRIES[city]
    for marker, code in COUNTRY_MARKERS.items():
        if re.search(r'\b' + re.escape(marker) + r'\b', text, re.I):
            return code
    return 'PL'


def parse_detail_page(html, api_event):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.angio_events')
    if not article:
        return None

    title = clean_text(article.select_one('h1')) or clean_text(api_event.get('title'))
    details = detail_values(article)
    event_date = parse_date(details.get('date', ''))
    description = clean_text(article.select_one('.event__text'))
    body_city, body_venue = body_location(description)

    venue = body_venue or details.get('venue', '')
    venue = re.sub(r'\s+\b(?:[01]?\d|2[0-3]):[0-5]\d\s*$', '', venue).strip()
    city = body_city or DEFAULT_CITY
    if not title or not event_date or not venue or not city:
        return None

    location_evidence = ' '.join(
        (body_city or '', details.get('address', ''), details.get('venue', ''), description[:500])
    )
    return {
        'title': title,
        'date': event_date,
        'url': api_event['url'],
        'time_from': parse_time(details.get('time', '')),
        'venue': venue,
        'city': city,
        'country_code': country_code(location_evidence, city),
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CapellaCracoviensisPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='capellacracoviensis_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = []
        page = 1
        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        '_fields': 'id,link,title',
                    },
                    timeout=45,
                )
                if response.status_code == 400 and page > 1:
                    break
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Capella Cracoviensis event index',
                    event='crawler_page_failed', level='warning', url=API_URL,
                    page=page, error_type=type(error).__name__, error_message=str(error),
                )
                break
            if not payload:
                break
            events.extend({
                'url': item['link'],
                'title': clean_text(item.get('title', {}).get('rendered', '')),
            } for item in payload if item.get('link'))
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        def load_detail(event):
            try:
                response = session.get(event['url'], timeout=45)
                response.raise_for_status()
                return parse_detail_page(response.text, event)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Capella Cracoviensis event',
                    event='crawler_page_failed', level='warning', url=event['url'],
                    error_type=type(error).__name__, error_message=str(error),
                )
                return None

        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(load_detail, event) for event in events]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']),
        )


def main():
    CapellaCracoviensisPlCrawler().run()


if __name__ == '__main__':
    main()
