import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lafilature.org/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programme')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'La Filature'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    lines = [' '.join(line.split()) for line in text.replace('\xa0', ' ').splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip('/') or '/', '', ''))


def fetch(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def iter_json_ld(value):
    if isinstance(value, list):
        for item in value:
            yield from iter_json_ld(item)
    elif isinstance(value, dict):
        if value.get('@type') == 'Event':
            yield value
        for key in ('@graph', 'itemListElement'):
            if key in value:
                yield from iter_json_ld(value[key])


def parse_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed


def detail_description(soup):
    parts = []
    for selector in (
        '.fiche-agenda__top__left-part__summary',
        '.fiche-agenda__top__left-part__body',
        '.fiche-agenda__bottom__programme',
        '.fiche-agenda__bottom__distribution__text',
    ):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    description = detail_description(soup)
    records = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            values = iter_json_ld(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue
        for event in values:
            start = parse_datetime(event.get('startDate'))
            location = event.get('location') or {}
            address = location.get('address') or {}
            title = clean_text(event.get('name'))
            venue = clean_text(location.get('name'))
            city = clean_text(address.get('addressLocality'))
            if not title or not start or not venue or not city:
                continue
            records.append({
                'title': title,
                'date': start.date().isoformat(),
                'url': canonical_url(event.get('url') or url),
                'time_from': start.strftime('%H:%M') if 'T' in event.get('startDate', '') else None,
                'venue': venue,
                'city': city,
                'country_code': 'FR',
                'description': description or clean_text(event.get('description')) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def programme_urls():
    urls = set()
    page = 1
    while True:
        separator = '&' if '?' in PROGRAMME_URL else '?'
        html = fetch(f'{PROGRAMME_URL}{separator}page={page}')
        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.agenda-preview a[href]')
        urls.update(canonical_url(card.get('href')) for card in cards)
        container = soup.select_one('.agenda-list__contents')
        if not cards or (container and container.has_attr('is-complete')):
            break
        page += 1
        if page > 100:
            raise RuntimeError('Programme pagination exceeded 100 pages')
    return urls


def sitemap_urls():
    soup = BeautifulSoup(fetch(SITEMAP_URL), 'xml')
    return {
        canonical_url(location.get_text(strip=True))
        for location in soup.select('url > loc')
        if location.get_text(strip=True).startswith(SOURCE_URL)
    }


class LaFilatureOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lafilature_org',
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
        urls = programme_urls() | sitemap_urls()
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(future.result(), url))
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape La Filature page',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        unique = {}
        for record in records:
            key = tuple(record[field] for field in ('title', 'date', 'time_from', 'venue', 'city'))
            unique[key] = record
        return sorted(
            unique.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    LaFilatureOrgCrawler().run()


if __name__ == '__main__':
    main()
