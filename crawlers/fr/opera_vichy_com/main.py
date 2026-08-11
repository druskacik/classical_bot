import json
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera-vichy.com/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda')
SOURCE = 'Opéra de Vichy'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    lines = [' '.join(line.split()) for line in text.replace('\xa0', ' ').splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def event_urls(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    urls = set()
    # Restrict discovery to the Drupal agenda view. The site navigation also
    # pins festival overview pages under /agenda/, but they are not occurrences
    # returned by the calendar feed.
    for link in soup.select('.views-results--agenda .views-row a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        path = urlparse(url).path.rstrip('/')
        if path.startswith('/agenda/') and path.count('/') == 2:
            urls.add(url.split('#', 1)[0])
    return sorted(urls)


def event_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M') if 'T' in str(value) else None


def description_from_page(soup, event):
    parts = []
    for selector in ('.field--name-field-editorial', '.field--name-field-colonne-informations'):
        for node in soup.select(selector):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)
    schema_description = clean_text(event.get('description'))
    if schema_description and not any(schema_description in part for part in parts):
        parts.insert(0, schema_description)
    return '\n\n'.join(parts) or None


def parse_event(page_html, requested_url):
    soup = BeautifulSoup(page_html, 'html.parser')
    event = event_json_ld(soup)
    if not event:
        return None

    occurrence = parse_datetime(event.get('startDate'))
    location = event.get('location') or {}
    address = location.get('address') or {}
    title = clean_text(event.get('name'))
    url = event.get('url') or requested_url
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country = clean_text(address.get('addressCountry')).upper()

    # This is a venue calendar based in Vichy. Some first-party records omit
    # addressLocality while still naming one of its Vichy venues explicitly.
    if not city and 'vichy' in venue.lower():
        city = 'Vichy'
    if not country:
        country = 'FR'
    if not occurrence or not title or not url or not venue or not city:
        return None

    event_date, time_from = occurrence
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city.title(),
        'country_code': country,
        'description': description_from_page(soup, event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OperaVichyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_vichy_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(AGENDA_URL, timeout=60)
            response.raise_for_status()
            urls = event_urls(response.text)
            records = []
            for url in urls:
                detail = session.get(url, timeout=60)
                detail.raise_for_status()
                record = parse_event(detail.text, url)
                if record:
                    records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Opéra de Vichy agenda',
                event='crawler_fetch_failed',
                level='error',
                url=getattr(getattr(error, 'request', None), 'url', AGENDA_URL),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OperaVichyComCrawler().run()


if __name__ == '__main__':
    main()
