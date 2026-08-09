import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cameratazuerich.ch/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concert'
SOURCE = 'Camerata Zürich'

HEADERS = {
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

SWISS_CITIES = (
    'Zürich', 'Basel', 'Bern', 'Luzern', 'Winterthur', 'St. Gallen',
    'Appenzell', 'Einsiedeln', 'Vitznau', 'Murten', 'Baden', 'Aarau', 'Schaffhausen',
    'Zug', 'Chur', 'Solothurn', 'Lausanne', 'Genève', 'Lugano',
)

# These venue names are used without a city on the organization's own pages.
ZURICH_VENUES = (
    'Tonhalle', 'Kulturhaus Helferei', 'Kirche Fraumünster', 'Muraltengut',
    'Kraftwerk Selnau', 'Musikschule Konservatorium', 'Kulturmarkt',
    'Kunsthaus Zürich',
)


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=60, **kwargs)
    response.raise_for_status()
    return response


def listing_events(session):
    records = []
    page = 1
    while True:
        response = get_response(
            session,
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'asc'},
        )
        batch = response.json()
        records.extend(batch)
        if page >= int(response.headers.get('X-WP-TotalPages', 1)):
            return records
        page += 1


def first_location_heading(soup, time_node):
    for node in time_node.find_all_next('div'):
        classes = node.get('class') or []
        if 'elementor-widget-heading' not in classes:
            continue
        text = clean_text(node)
        if text and text.lower() != 'programm':
            return text
    return ''


def parse_location(value, title):
    time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', value)
    if not time_match:
        return None

    time_from = time_match.group(0)
    venue = value[:time_match.start()].strip(' ,\u2013-')
    if not venue:
        return None

    search_text = f'{venue} {title}'
    if re.search(r'\b(?:Istanbul|İstanbul)\b', search_text, re.I):
        return venue, 'Istanbul', 'TR', time_from

    city = next(
        (candidate for candidate in SWISS_CITIES
         if re.search(rf'(?<!\w){re.escape(candidate)}(?!\w)', search_text, re.I)),
        '',
    )
    if not city and any(name.lower() in venue.lower() for name in ZURICH_VENUES):
        city = 'Zürich'
    if not city:
        return None
    return venue, city, 'CH', time_from


def event_description(event, soup):
    parts = []
    program = soup.select_one('.concert-program-container')
    program_text = clean_text(program)
    if program_text:
        parts.append(program_text)

    body_text = clean_text((event.get('content') or {}).get('rendered'))
    if body_text and body_text not in parts:
        parts.append(body_text)
    return '\n\n'.join(parts) or None


def make_record(event, page_html):
    title = clean_text((event.get('title') or {}).get('rendered'))
    url = event.get('link') or ''
    soup = BeautifulSoup(page_html, 'html.parser')
    time_nodes = soup.select('time')
    if not title or not url or not time_nodes:
        return None

    try:
        event_date = datetime.strptime(
            clean_text(time_nodes[0]), '%d/%m/%Y'
        ).date().isoformat()
    except ValueError:
        return None

    location_text = first_location_heading(soup, time_nodes[-1])
    location = parse_location(location_text, title)
    if not location:
        return None
    venue, city, country_code, time_from = location

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': event_description(event, soup),
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_response, session, event.get('link', '')): event
            for event in events
            if event.get('link')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                record = make_record(event, future.result().text)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Camerata Zürich concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
    )


class CamerataZuerichChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cameratazuerich_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CamerataZuerichChCrawler().run()


if __name__ == '__main__':
    main()
