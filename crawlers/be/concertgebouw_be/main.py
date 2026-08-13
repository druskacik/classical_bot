import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.concertgebouw.be/'
SOURCE = 'Concertgebouw Brugge'
PROGRAMME_URL = f'{SOURCE_URL}nl/programma'
GENRES = ('muziek', 'families', 'dans', 'klankkunst', 'muziektheater')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-BE,nl;q=0.9,en;q=0.5',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', html.unescape(str(value)).replace('\xa0', ' ')).strip()


def event_links(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    links = []
    for card in soup.select('article[data-component="card--wide"]'):
        link = card.find('a', href=True)
        date_node = card.find('time', attrs={'datetime': True})
        title_node = card.find('h3')
        if not all((link, date_node, title_node)):
            continue
        links.append({
            'url': urljoin(SOURCE_URL, link['href']),
            'listing_title': clean_text(title_node),
            'listing_datetime': date_node['datetime'],
        })
    return links


def parse_json_ld(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if isinstance(entry, dict) and entry.get('@type') in ('Event', 'MusicEvent', 'DanceEvent'):
                return entry
    return None


def programme_text(soup):
    heading = next(
        (node for node in soup.find_all(['h2', 'h3']) if clean_text(node).lower() == 'programma'),
        None,
    )
    if heading is None:
        return ''
    container = heading.find_parent('div', class_='js-toggle--container')
    if container is None:
        return ''
    # The expanded and collapsed blocks duplicate the programme. Prefer the
    # complete hidden block and keep the text confined to this section.
    content = container.select_one('.js-toggle--content.u-hidden')
    content = content or container.select_one('.js-toggle--content')
    return clean_text(content)


def parse_detail(page_html, fallback):
    soup = BeautifulSoup(page_html, 'html.parser')
    event = parse_json_ld(soup)
    if not event:
        return None

    start = str(event.get('startDate') or fallback['listing_datetime'])
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?', start)
    if not match:
        return None
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return None

    location = event.get('location') if isinstance(event.get('location'), dict) else {}
    address = location.get('address', '')
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
        address_text = clean_text(' '.join(str(value) for value in address.values()))
    else:
        address_text = clean_text(address)
        city = ''
    if not city and re.search(r'\bBrugge\b', address_text, re.IGNORECASE):
        city = 'Brugge'

    title = clean_text(event.get('name')) or fallback['listing_title']
    venue = clean_text(location.get('name'))
    canonical_url = urljoin(SOURCE_URL, clean_text(event.get('url')) or fallback['url'])
    description_parts = [clean_text(event.get('description')), programme_text(soup)]
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None
    if not all((title, venue, city, canonical_url)):
        return None
    return {
        'title': title,
        'date': match.group(1),
        'url': canonical_url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': 'BE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ConcertgebouwBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='concertgebouw_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def _fetch_listings(self, session):
        found = {}
        for genre in GENRES:
            for page_number in range(100):
                url = f'{PROGRAMME_URL}/term_genre_and_style={genre}/page={page_number}'
                try:
                    response = session.get(url, timeout=45)
                    response.raise_for_status()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Concertgebouw programme page',
                        event='crawler_fetch_failed', level='error', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    break
                links = event_links(response.text)
                if not links:
                    break
                new_count = 0
                for item in links:
                    if item['url'] not in found:
                        new_count += 1
                    found[item['url']] = item
                if len(links) < 12 or new_count == 0:
                    break
        return list(found.values())

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        listings = self._fetch_listings(session)
        records = []

        def fetch(item):
            response = session.get(item['url'], timeout=45)
            response.raise_for_status()
            return parse_detail(response.text, item)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, item): item for item in listings}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Concertgebouw event',
                        event='crawler_fetch_failed', level='warning', url=item['url'],
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        records.sort(key=lambda row: (row['date'], row['time_from'] or '', row['title']))
        return records


def main():
    return ConcertgebouwBeCrawler().run()


if __name__ == '__main__':
    main()
