import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.udk-berlin.de/'
CALENDAR_API = urljoin(SOURCE_URL, 'api/v1/kalender')
SOURCE = 'Universität der Künste Berlin'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# Student recitals are normally labelled Vortragsabend rather than Konzert.
CONCERT_KINDS = {
    'Konzert',
    'Matinee',
    'Symphoniekonzert',
    'Vortragsabend',
    'Vortragsnachmittag',
    'Werkstattabend',
}

MONTHS = {
    'Januar': 1, 'Februar': 2, 'Maerz': 3, 'März': 3, 'April': 4,
    'Mai': 5, 'Juni': 6, 'Juli': 7, 'August': 8, 'September': 9,
    'Oktober': 10, 'November': 11, 'Dezember': 12,
}

COUNTRIES = {
    'Deutschland': 'DE', 'Germany': 'DE',
    'Tschechien': 'CZ', 'Czechia': 'CZ', 'Czech Republic': 'CZ',
    'Oesterreich': 'AT', 'Österreich': 'AT', 'Austria': 'AT',
    'Schweiz': 'CH', 'Switzerland': 'CH',
    'Polen': 'PL', 'Poland': 'PL',
    'Frankreich': 'FR', 'France': 'FR',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def calendar_results(session):
    page = 1
    results = []
    while True:
        response = session.get(CALENDAR_API, params={'search[page]': page}, timeout=45)
        response.raise_for_status()
        data = response.json()['content']['colPos0'][0]['content']['data']['documents']
        results.extend(data.get('list', {}).get('results') or [])
        if page >= data.get('pagination', {}).get('numberOfPages', page):
            break
        page += 1
    return results


def parse_datetime(text):
    text = clean_text(text)
    match = re.search(
        r'(\d{1,2})\.\s*(' + '|'.join(map(re.escape, MONTHS)) +
        r')\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?', text,
    )
    if not match:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    event_time = f'{int(match.group(4)):02d}:{match.group(5)}' if match.group(4) else None
    return event_date, event_time


def parse_location(text):
    location = clean_text(text).replace('\n', ', ')
    location = re.sub(r'\s*,\s*', ', ', location).strip(' ,')
    if not location:
        return None, None, None

    country_code = 'DE'
    for country_name, code in COUNTRIES.items():
        if re.search(rf'\b{re.escape(country_name)}\b', location, re.I):
            country_code = code
            break

    postal_city = re.search(r'\b(?:\d{5}|\d{3}\s?\d{2}|\d{4})\s+([^,]+)', location)
    city = clean_text(postal_city.group(1)) if postal_city else None
    if city:
        city = re.sub(
            r'\s+(?:' + '|'.join(map(re.escape, COUNTRIES)) + r')$', '', city,
            flags=re.I,
        ).strip()
    if not city and re.search(r'\bBerlin\b', location, re.I):
        city = 'Berlin'

    # The first comma-separated component is the site's venue name; later
    # components are an address and must not leak into the venue field.
    venue = clean_text(location.split(',', 1)[0])
    if not venue or not city:
        return None, None, None
    return venue, city, country_code


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('.h-event') or soup.select_one('main')
    if not event:
        return None

    heading = event.select_one('h1.page-title') or event.select_one('h1')
    title = clean_text(heading)
    title = re.sub(r'\s+[\u2013-]\s+(?:' + '|'.join(map(re.escape, CONCERT_KINDS)) + r')$', '', title)
    date_node = event.select_one('.date')
    event_date, event_time = parse_datetime(date_node.get_text(' ', strip=True) if date_node else '')
    location_node = event.select_one('.p-location')
    venue, city, country_code = parse_location(location_node if location_node else '')
    if not title or not event_date or not venue or not city:
        return None

    sections = [clean_text(node) for node in event.select('.detail-section')]
    sections = [section for section in sections if section]
    description = clean_text('\n\n'.join(dict.fromkeys(sections))) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    listings = calendar_results(session)
    urls = {
        urljoin(SOURCE_URL, item['url'])
        for item in listings
        if clean_text(item.get('subtitle')) in CONCERT_KINDS and item.get('url')
    }
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                response = future.result()
                response.raise_for_status()
                record = parse_event(response.text, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class UdkBerlinDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='udk_berlin_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    UdkBerlinDeCrawler().run()


if __name__ == '__main__':
    main()
