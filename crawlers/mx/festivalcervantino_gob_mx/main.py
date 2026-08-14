import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, unquote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://festivalcervantino.gob.mx/'
SOURCE = 'Festival Internacional Cervantino'
COUNTRY_CODE = 'MX'

# These are the site's first-party performance disciplines. They deliberately
# form a candidate feed: each is mixed, and eligible opera, ballet, live-score,
# crossover, and art-music performances are not confined to /musica.
FEED_PATHS = (
    'musica',
    'opera',
    'danza',
    'multidisciplina',
    'teatro',
    'de-calle',
    'actividades-formativas',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-MX,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11,
    'dic': 12,
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def listing_urls(session):
    urls = set()
    for path in FEED_PATHS:
        url = urljoin(SOURCE_URL, path)
        soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
        for link in soup.select('a[href*="/actividad/"][href]'):
            event_url = urljoin(url, link.get('href', '')).split('?', 1)[0]
            if re.search(r'/actividad/\d+/', event_url):
                urls.add(event_url)
    return sorted(urls)


def event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    # Some pages embed literal newlines in JSON string values, which makes the
    # otherwise useful JSON-LD invalid. The equivalent first-party metadata is
    # stable, and startDate itself can still be recovered from the script.
    title_meta = soup.select_one('meta[property="og:title"]')
    description_meta = soup.select_one('meta[name="description"]')
    script_text = '\n'.join(script.get_text() for script in soup.select(
        'script[type="application/ld+json"]'
    ))
    start = re.search(r'"startDate"\s*:\s*"([^"]+)"', script_text)
    return {
        '@type': 'Event',
        'name': title_meta.get('content', '') if title_meta else '',
        'description': description_meta.get('content', '') if description_meta else '',
        'startDate': start.group(1) if start else '',
    }


def event_year(data):
    match = re.match(r'(20\d{2})-', str(data.get('startDate') or ''))
    return int(match.group(1)) if match else None


def parse_schedule(value, year):
    """Expand strings such as '03 oct | 11, 12, 13:30 h'."""
    match = re.search(r'(\d{1,2})\s+([a-z]{3,4})\s*\|\s*(.+?)\s*h\b', value.lower())
    if not match or not year:
        return []
    month = MONTHS.get(match.group(2))
    try:
        event_date = date(year, month, int(match.group(1))).isoformat()
    except (TypeError, ValueError):
        return []
    times = []
    for hour, minute in re.findall(r'(\d{1,2})(?::(\d{2}))?', match.group(3)):
        if int(hour) < 24:
            times.append(f'{int(hour):02d}:{minute or "00"}')
    return [(event_date, value) for value in dict.fromkeys(times)]


def venue_city(session, venue_url):
    soup = BeautifulSoup(get_response(session, venue_url).text, 'html.parser')
    iframe = soup.select_one('iframe[src*="maps.google.com/maps"]')
    if iframe:
        query = parse_qs(urlparse(iframe.get('src', '')).query).get('q', [''])[0]
        address = unquote_plus(query)
        parts = re.split(r'<br\s*/?>|\n', address, flags=re.I)
        if len(parts) >= 2:
            city = clean_text(parts[-1]).split(',', 1)[0].strip()
            if city:
                return city
    # The primary festival programme is venue-based in Guanajuato city. This
    # fallback is used only for its own recinto pages, not the touring circuit.
    return 'Guanajuato'


def schedule_blocks(soup):
    for link in soup.select('.ficha-tecnica a[href*="/recinto/"][href]'):
        container = link.find_parent(class_='ficha-etiqueta') or link.parent
        schedule = clean_text(container.find('div') if container else None)
        venue = clean_text(link)
        venue_url = urljoin(SOURCE_URL, link.get('href', ''))
        if schedule and venue and venue_url:
            yield schedule, venue, venue_url


def detail_records(session, url, city_cache):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    data = event_data(soup)
    title = clean_text(data.get('name'))
    description = clean_text(data.get('description')) or None
    year = event_year(data)
    if not title or not year:
        return []

    records = []
    for schedule, venue, venue_url in schedule_blocks(soup):
        if venue_url not in city_cache:
            city_cache[venue_url] = venue_city(session, venue_url)
        city = city_cache[venue_url]
        if not city:
            continue
        for event_date, time_from in parse_schedule(schedule, year):
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': COUNTRY_CODE,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    city_cache = {}

    # Venue lookups share only an idempotent cache; duplicate requests are
    # harmless and final records are deduplicated below.
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_records, session, url, city_cache): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Festival Cervantino event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class FestivalCervantinoGobMxCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalcervantino_gob_mx',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    FestivalCervantinoGobMxCrawler().run()


if __name__ == '__main__':
    main()
