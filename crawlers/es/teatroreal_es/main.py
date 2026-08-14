import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatroreal.es/es'
CURRENT_SEASON_URL = f'{SOURCE_URL}/temporada-actual'
SOURCE = 'Teatro Real'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}

# These are spaces operated by the Madrid-based Teatro Real. Unknown spaces
# are deliberately not assigned Madrid, since archived pages can include tours.
MADRID_VENUE_MARKERS = (
    'teatro real',
    'real teatro de retiro',
    'sala principal',
    'sala gayarre',
    'salon de baile',
    'salón de baile',
    'plaza de oriente',
    'plaza de isabel ii',
)


def clean_text(value):
    if not value:
        return ''
    raw = unescape(str(value))
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def canonical_url(value):
    url = urljoin(SOURCE_URL, clean_text(value))
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def season_urls(session):
    soup = get_soup(session, CURRENT_SEASON_URL)
    urls = {CURRENT_SEASON_URL}
    for option in soup.select('select.js-viewsJumpMenu option[data-url]'):
        value = clean_text(option.get('data-url'))
        if '/es/temporada/' in value:
            urls.add(canonical_url(value))
    return sorted(urls)


def detail_urls(session):
    urls = set()
    for season_url in season_urls(session):
        try:
            soup = get_soup(session, season_url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape season page',
                event='crawler_item_failed',
                level='warning',
                url=season_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for link in soup.select('a[href*="/es/espectaculo/"]'):
            url = canonical_url(link.get('href'))
            if re.search(r'/es/espectaculo/[^/]+$', url):
                urls.add(url)
    return sorted(urls)


def event_json(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload.get('@graph', []) if isinstance(payload, dict) else []
        if isinstance(payload, dict):
            candidates = [payload, *candidates]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return {}


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([A-Za-záéíóúñ]+)\s+(\d{4})', clean_text(value).casefold())
    if not match:
        return None
    month = MONTHS.get(match.group(2))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)', clean_text(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def city_for_venue(venue):
    normalized = clean_text(venue).casefold()
    if 'madrid' in normalized or any(marker in normalized for marker in MADRID_VENUE_MARKERS):
        return 'Madrid'
    return None


def parse_detail(session, url):
    soup = get_soup(session, url)
    event = event_json(soup)
    title = clean_text(event.get('name')) or clean_text(soup.select_one('h1'))
    description = clean_text(event.get('description')) or None
    event_url = canonical_url(event.get('url') or url)
    records = []

    for occurrence in soup.select('.functions-show__block'):
        event_date = parse_date(occurrence.select_one('.functions-show__block--item-date'))
        time_from = parse_time(occurrence.select_one('.functions-show__block--item-hour'))
        venue = clean_text(occurrence.select_one('.functions-show__block--item-space'))
        city = city_for_venue(venue)
        if not all((title, event_date, event_url, venue, city)):
            log_message(
                'Skipping occurrence with incomplete required fields',
                event='crawler_item_skipped',
                level='warning',
                url=url,
                missing_title=not bool(title),
                missing_date=not bool(event_date),
                missing_venue=not bool(venue),
                missing_city=not bool(city),
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': event_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


class TeatroRealEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatroreal_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='potential',
        columns=['title', 'date', 'url', 'time_from', 'venue', 'city', 'description'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = detail_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(parse_detail, session, url): url for url in urls}
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
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    TeatroRealEsCrawler().run()


if __name__ == '__main__':
    main()
