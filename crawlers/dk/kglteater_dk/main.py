import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kglteater.dk/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Det Kongelige Teater'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}

# These are the theatre's Copenhagen stages and outdoor performance spaces.
# Touring productions are not assigned the theatre's home city.
COPENHAGEN_VENUE_PREFIXES = (
    'A-salen',
    'Gamle Scene',
    'Kongens Nytorv',
    'Kyssetrappen',
    'Operaen',
    'Ofelia Plads',
    'Skuespilhuset',
    'Takkelloftet',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_inline(value):
    return re.sub(r'\s+', ' ', clean_text(value)).strip()


def get_response(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response


def production_urls(session):
    """Return every concert production which remains in the public sitemap."""
    text = get_response(session, SITEMAP_URL).text
    urls = re.findall(r'<loc>(https://www\.kglteater\.dk/koncert/\d{4}/[^<]+)</loc>', text)
    return sorted(set(urls))


def city_for_venue(venue):
    normalized = clean_text(venue)
    if normalized.startswith(COPENHAGEN_VENUE_PREFIXES):
        return 'København'

    # Some off-site listings include the city explicitly after a comma. Only
    # accept that structured form; unknown tour venues are skipped.
    if ',' in normalized:
        candidate = clean_text(normalized.rsplit(',', 1)[1])
        if re.fullmatch(r"[A-Za-zÆØÅæøåÉéÜü .'-]{2,60}", candidate):
            return candidate
    return None


def event_nodes(soup):
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data.get('@graph', []) if isinstance(data, dict) else []
        if isinstance(data, dict) and data.get('startDate'):
            candidates = [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('startDate'):
                nodes.append(candidate)
    return nodes


def production_description(soup):
    parts = []
    for selector in (
        '[data-testid="show-page-lead"]',
        '[data-testid="show-page-description"]',
    ):
        value = clean_text(soup.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def parse_start(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def archived_records(soup, url, description):
    """Parse expired productions after ticketing JSON-LD has been removed."""
    title = clean_inline(soup.select_one('[data-testid="show-page-hero-title"]'))
    location = clean_text(
        soup.select_one('[data-testid="show-page-production-location"]')
    )
    stage = clean_text(soup.select_one('[data-testid="show-page-production-stage"]'))
    venue = ', '.join(part for part in (location, stage) if part)
    city = city_for_venue(venue)

    dates = {
        node.get('datetime')
        for node in soup.select(
            '[data-testid="show-page-hero-date"] time[datetime], '
            '[data-testid="show-page-sticky-date-value"] time[datetime]'
        )
    }
    # Multi-date archive pages expose the exact former dates in cast metadata.
    for node in soup.select('[data-roles-ctx]'):
        try:
            context = json.loads(node.get('data-roles-ctx'))
        except (json.JSONDecodeError, TypeError):
            continue
        dates.update((context.get('dates') or {}).keys())

    records = []
    if not title or not venue or not city:
        return records
    for value in dates:
        try:
            event_date = datetime.strptime(value, '%Y-%m-%d').date().isoformat()
        except (TypeError, ValueError):
            continue
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': None,
                'venue': venue,
                'city': city,
                'country_code': 'DK',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def scrape_production(session, url):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    description = production_description(soup)
    records = []
    for event in event_nodes(soup):
        start = parse_start(event.get('startDate'))
        location = event.get('location')
        venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
        city = city_for_venue(venue)
        title = clean_inline(event.get('name'))
        event_url = event.get('url') or url
        if not start or not title or not venue or not city or not event_url:
            continue
        records.append(
            {
                'title': title,
                'date': start.date().isoformat(),
                'url': urljoin(SOURCE_URL, event_url),
                'time_from': start.strftime('%H:%M'),
                'venue': venue,
                'city': city,
                'country_code': 'DK',
                'description': description or clean_text(event.get('description')) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records or archived_records(soup, url, description)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = production_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(scrape_production, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert production',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class KglteaterDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kglteater_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
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
        return get_concerts()


def main():
    KglteaterDkCrawler().run()


if __name__ == '__main__':
    main()
