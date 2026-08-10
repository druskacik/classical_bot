import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivaldetorroella.cat/'
PROGRAM_URL = urljoin(SOURCE_URL, 'ca/programacio.html')
SOURCE = 'Festival de Torroella de Montgrí'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ca-ES,ca;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response


def listing_urls():
    urls = set()
    # The historic view contains finished events still published by the site;
    # the normal view contains upcoming events. There is some intentional overlap.
    for url in (PROGRAM_URL, f'{PROGRAM_URL}?historic'):
        soup = BeautifulSoup(get_response(url).text, 'html.parser')
        for link in soup.select('a[href*="/programacio/c/"]'):
            event_url = urljoin(SOURCE_URL, link.get('href', ''))
            if re.search(r'/programacio/c/\d+-[^/]+\.html$', event_url):
                urls.add(event_url)
    return sorted(urls)


def event_objects(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (json.JSONDecodeError, TypeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict) and value.get('@type') == 'Event':
                yield value


def parse_event(url):
    soup = BeautifulSoup(get_response(url).text, 'html.parser')
    records = []
    for event in event_objects(soup):
        title = clean_text(event.get('name'))
        location = event.get('location') if isinstance(event.get('location'), dict) else {}
        address = location.get('address') if isinstance(location.get('address'), dict) else {}
        venue = clean_text(location.get('name'))
        city = clean_text(address.get('addressLocality'))
        country_code = clean_text(address.get('addressCountry')).upper() or 'ES'
        event_url = urljoin(SOURCE_URL, event.get('url') or url)
        try:
            start = datetime.fromisoformat(str(event.get('startDate')).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            continue
        if not title or not event_url or not venue or not city or len(country_code) != 2:
            continue
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': event_url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': clean_text(event.get('description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_event, url): url for url in listing_urls()}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class FestivalDeTorroellaCatCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivaldetorroella_cat',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
    FestivalDeTorroellaCatCrawler().run()


if __name__ == '__main__':
    main()
