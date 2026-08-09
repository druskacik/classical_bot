import json
import re
from datetime import datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ultraschallberlin.de/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Ultraschall Berlin'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def get_text(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def concert_urls(session):
    root = ElementTree.fromstring(get_text(session, SITEMAP_URL))
    urls = []
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1] != 'loc' or not element.text:
            continue
        url = element.text.strip()
        parsed = urlparse(url)
        if parsed.hostname == 'ultraschallberlin.de' and parsed.path.startswith('/konzert/'):
            urls.append(url)
    return sorted(set(urls))


def event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload.get('@graph', []) if isinstance(payload, dict) else []
        if isinstance(payload, dict) and payload.get('@type') == 'Event':
            nodes = [payload]
        for node in nodes:
            node_types = node.get('@type', []) if isinstance(node, dict) else []
            if isinstance(node_types, str):
                node_types = [node_types]
            if 'Event' in node_types:
                return node
    return None


def resolve_location(event):
    locations = event.get('location') or []
    if isinstance(locations, dict):
        locations = [locations]
    for location in locations:
        if not isinstance(location, dict):
            continue
        venue = clean_text(location.get('name'))
        address = location.get('address') or {}
        city = clean_text(address.get('addressLocality')) if isinstance(address, dict) else ''
        country = clean_text(address.get('addressCountry')) if isinstance(address, dict) else ''
        if venue and city and (not country or country.upper() in ('DE', 'DEU', 'GERMANY', 'DEUTSCHLAND')):
            return venue, city
    return None, None


def page_description(soup, event):
    main = soup.select_one('main')
    if main:
        content = BeautifulSoup(str(main), 'html.parser')
        for selector in (
            'header.entry-header', '.fotos', '.gallery', '.tickets',
            '.radiosendungen', 'script', 'style', 'noscript',
        ):
            for element in content.select(selector):
                element.decompose()
        description = clean_text(content)
        if description:
            return description
    return clean_text(event.get('description')) or None


def event_start(event, soup):
    start = event.get('startDate')
    try:
        return datetime.fromisoformat(start.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        # One-off events may contain a localized date in otherwise structured
        # JSON-LD. The visible event header remains consistently parseable.
        header = clean_text(soup.select_one('.datumort'))
        match = re.search(
            r'(\d{2})\.(\d{2})\.(\d{4})(?:\s+um\s+(\d{1,2}):(\d{2})\s+Uhr)?',
            header,
        )
        if not match:
            return None
        hour = int(match.group(4)) if match.group(4) else 0
        minute = int(match.group(5)) if match.group(5) else 0
        try:
            return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)), hour, minute)
        except ValueError:
            return None


def make_record(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    event = event_data(soup)
    if not event:
        return None

    title = clean_text(event.get('name'))
    venue, city = resolve_location(event)
    start_at = event_start(event, soup)
    if not title or not start_at or not venue or not city:
        return None

    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'description': page_description(soup, event),
    }


def get_concerts():
    session = make_session()
    records = []
    for url in concert_urls(session):
        try:
            record = make_record(url, get_text(session, url))
        except (requests.RequestException, ElementTree.ParseError) as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
        else:
            log_message(
                'Concert detail lacks required structured fields',
                event='crawler_item_skipped',
                level='warning',
                url=url,
            )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'], record['title'], record['url']),
    )


class UltraschallBerlinDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ultraschallberlin_de',
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
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    UltraschallBerlinDeCrawler().run()


if __name__ == '__main__':
    main()
