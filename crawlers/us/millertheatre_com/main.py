import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.millertheatre.com/'
SITEMAP_URL = f'{SOURCE_URL}sitemaps-1-section-events-1-sitemap.xml'
SOURCE = 'Miller Theatre at Columbia University'
CITY = 'New York'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})'
    r'(?:,\s*(\d{1,2}(?::\d{2})?\s*[AP]M))?',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    urls = []
    for element in root.iter():
        if not element.tag.endswith('loc') or not element.text:
            continue
        url = element.text.strip()
        parsed = urlparse(url)
        if (
            parsed.netloc == 'www.millertheatre.com'
            and parsed.path.startswith('/events/')
            and parsed.path != '/events/'
        ):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_datetime(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None

    time_from = None
    if match.group(2):
        normalized = re.sub(r'\s+', ' ', match.group(2).upper()).strip()
        normalized = re.sub(r'(?<=\d)([AP]M)$', r' \1', normalized)
        for pattern in ('%I:%M %p', '%I %p'):
            try:
                time_from = datetime.strptime(normalized, pattern).strftime('%H:%M')
                break
            except ValueError:
                continue
    return event_date, time_from


def schema_event(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        nodes = payload.get('@graph', []) if isinstance(payload, dict) else []
        if isinstance(payload, dict) and payload.get('@type') in {'Event', 'MusicEvent', 'TheaterEvent'}:
            nodes = [payload]
        for node in nodes:
            if isinstance(node, dict) and node.get('@type') in {
                'Event', 'MusicEvent', 'TheaterEvent'
            }:
                return node
    return {}


def event_description(soup, schema):
    parts = []
    hero = soup.select_one('.hide-lt-md .event-hero__text') or soup.select_one(
        '.event-hero__text'
    )
    info = soup.select_one('.event-info_primary')
    for value in (
        hero.get_text('\n', strip=True) if hero else '',
        info.get_text('\n', strip=True) if info else '',
        schema.get('description'),
    ):
        text = clean_text(value)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event_html(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    schema = schema_event(soup)
    title_node = soup.select_one('.event-hero h1') or soup.select_one('h1')
    date_node = soup.select_one('.event-hero__date')
    venue_node = soup.select_one('.event-hero__location')

    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    event_date, time_from = parse_datetime(
        date_node.get_text(' ', strip=True) if date_node else ''
    )
    if not event_date and schema.get('startDate'):
        try:
            event_date = datetime.fromisoformat(
                str(schema['startDate']).replace('Z', '+00:00')
            ).date().isoformat()
        except ValueError:
            event_date = None
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')

    if not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': event_description(soup, schema),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event_html(response.text, response.url)


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_event, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Miller Theatre event request failed',
                    event='crawler_detail_failed',
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
                    'Skipping event without required fields',
                    event='crawler_record_skipped',
                    level='warning',
                    url=url,
                )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class MillerTheatreComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='millertheatre_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
        return scrape_events()


def main():
    MillerTheatreComCrawler().run()


if __name__ == '__main__':
    main()
