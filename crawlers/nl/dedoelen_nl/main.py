import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dedoelen.nl/'
AGENDA_URL = urljoin(SOURCE_URL, 'nl/agenda')
SOURCE = 'de Doelen'
DEFAULT_CITY = 'Rotterdam'
DEFAULT_VENUE = 'de Doelen'

# A deliberately broad range makes the server return both its retained archive
# and announced future events. The site currently retains roughly one year of
# past listings, and applies these parameters consistently to every result page.
AGENDA_PARAMS = {'start': '2000-01-01', 'end': '2100-12-31'}
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def fetch(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def agenda_urls(session):
    urls = set()
    page = 1
    last_page = 1
    while page <= last_page:
        params = {**AGENDA_PARAMS, 'page': page}
        soup = BeautifulSoup(fetch(session, AGENDA_URL, params=params).text, 'html.parser')
        page_values = []
        for option in soup.select('select[name="page"] option[value]'):
            try:
                page_values.append(int(option['value']))
            except ValueError:
                continue
        last_page = max(page_values, default=last_page)

        for anchor in soup.select('main a[href]'):
            url = urljoin(SOURCE_URL, anchor.get('href'))
            if re.fullmatch(r'/nl/agenda/[^/]+', urlparse(url).path.rstrip('/')):
                urls.add(url)
        page += 1
    return sorted(urls)


def schema_events(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        events.extend(
            item for item in items
            if isinstance(item, dict) and item.get('@type') == 'Event'
        )
    return events


def description_from_page(soup, event):
    parts = []
    for node in soup.select('main .richtext'):
        # Suggestions and the newsletter are separate components and do not use
        # the event-body richtext blocks.
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    fallback = clean_text(event.get('description'))
    if fallback and not parts:
        parts.append(fallback)
    return '\n\n'.join(parts) or None


def occurrence_venues(soup):
    return [clean_text(box.select_one('.venue')) for box in soup.select('main .box')]


def event_venue(event, venue_from_box):
    if venue_from_box:
        return venue_from_box
    location = event.get('location') or {}
    if isinstance(location, dict):
        venue = clean_text(location.get('name'))
        if venue:
            return venue
    return DEFAULT_VENUE


def make_record(event, url, venue, description):
    title = clean_text(event.get('name'))
    raw_start = event.get('startDate')
    try:
        start = datetime.fromisoformat(str(raw_start).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if not title or not venue:
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': DEFAULT_CITY,
        'country_code': 'NL',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(session, url):
    response = fetch(session, url)
    soup = BeautifulSoup(response.text, 'html.parser')
    events = schema_events(soup)
    venues = occurrence_venues(soup)
    records = []
    for index, event in enumerate(events):
        venue = event_venue(event, venues[index] if index < len(venues) else '')
        record = make_record(event, response.url, venue, description_from_page(soup, event))
        if record:
            records.append(record)
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = agenda_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue'], item['url']
    ))


class DedoelenNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dedoelen_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
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
    DedoelenNlCrawler().run()


if __name__ == '__main__':
    main()
