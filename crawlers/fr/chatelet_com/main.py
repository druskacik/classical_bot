import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chatelet.com/'
SITEMAP_URL = f'{SOURCE_URL}evenements-sitemap.xml'
SOURCE = 'Théâtre du Châtelet'
DEFAULT_CITY = 'Paris'
DEFAULT_VENUE = 'Théâtre du Châtelet'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    lines = [' '.join(line.replace('\xa0', ' ').split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def canonical_url(value):
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def event_urls_from_sitemap(xml):
    soup = BeautifulSoup(xml, 'xml')
    urls = []
    for element in soup.select('url > loc'):
        url = canonical_url(clean_text(element))
        path = urlsplit(url).path
        if path.startswith('/programmation/') and path.count('/') >= 4:
            urls.append(url)
    return sorted(set(urls))


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data.get('@graph', []) if isinstance(data, dict) and '@graph' in data else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def description_from_page(soup, schema):
    parts = []
    summary = clean_text(schema.get('description'))
    if summary and summary != 'No Information':
        parts.append(summary)
    for element in soup.select('main .editor-content, main .infos-item-content'):
        text = clean_text(element)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def occurrences(schema):
    children = schema.get('subEvents') or []
    if isinstance(children, dict):
        children = [children]
    return children or [schema]


def parse_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return []
    title = clean_text(schema.get('name')) or clean_text(soup.select_one('main h1'))
    description = description_from_page(soup, schema)
    records = []
    for occurrence in occurrences(schema):
        start = occurrence.get('startDate')
        try:
            parsed_start = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            continue
        location = occurrence.get('location') or schema.get('location') or {}
        venue = clean_text(location.get('name')) or DEFAULT_VENUE
        address = location.get('address') or {}
        city = clean_text(address.get('addressLocality')) if isinstance(address, dict) else ''
        city = city or DEFAULT_CITY
        if not title or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': parsed_start.date().isoformat(),
            'url': url,
            'time_from': parsed_start.strftime('%H:%M') if 'T' in start else None,
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_page(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_page(response.text, url)


class ChateletComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chatelet_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        urls = event_urls_from_sitemap(response.text)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_page, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Châtelet event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    ChateletComCrawler().run()


if __name__ == '__main__':
    main()
