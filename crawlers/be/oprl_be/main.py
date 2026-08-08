import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oprl.be/fr'
CONCERTS_URL = f'{SOURCE_URL}/concerts'
SOURCE = 'Orchestre Philharmonique Royal de Liège'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-BE,fr;q=0.9,en;q=0.7',
}

# Event schema on the site normally exposes the location as "city, venue" but
# not its country. OPRL tours, so explicitly resolve the cities which occur in
# its calendar instead of applying its Liège home-country to every event.
CITY_COUNTRIES = {
    # Belgium
    'anvers': 'BE', 'antwerpen': 'BE', 'arlon': 'BE', 'ath': 'BE', 'aubel': 'BE',
    'bruxelles': 'BE', 'brussel': 'BE', 'charleroi': 'BE', 'eupen': 'BE',
    'flagey': 'BE', 'gand': 'BE', 'gent': 'BE', 'hasselt': 'BE',
    'bruges': 'BE', 'brugge': 'BE', 'huy': 'BE', 'la louvière': 'BE',
    'liège': 'BE', 'louvain': 'BE', 'louvain-la-neuve': 'BE', 'leuven': 'BE',
    'malmedy': 'BE', 'mons': 'BE', 'namur': 'BE',
    'ostende': 'BE', 'oostende': 'BE', 'saint-hubert': 'BE',
    'saint-vith': 'BE', 'spa': 'BE', 'stavelot': 'BE', 'tournai': 'BE',
    'turnhout': 'BE', 'verviers': 'BE', 'waimes': 'BE', 'waterloo': 'BE',
    'welkenraedt': 'BE',
    # Recurring international tour destinations
    'aix-la-chapelle': 'DE', 'aachen': 'DE', 'berlin': 'DE', 'cologne': 'DE',
    'köln': 'DE', 'dortmund': 'DE', 'bad kissingen': 'DE',
    'amsterdam': 'NL', 'maastricht': 'NL', 'utrecht': 'NL',
    'luxembourg': 'LU', 'esch-sur-alzette': 'LU', 'paris': 'FR',
    'aix-en-provence': 'FR', 'besançon': 'FR', 'dole': 'FR', 'lille': 'FR',
    'metz': 'FR', 'montpellier': 'FR', 'reims': 'FR', 'saint-émilion': 'FR',
    'vienne': 'AT', 'wien': 'AT',
    'londres': 'GB', 'london': 'GB', 'genève': 'CH', 'geneva': 'CH',
    'budapest': 'HU', 'pécs': 'HU', 'veszprém': 'HU', 'ostrava': 'CZ',
    'wroclaw': 'PL', 'wrocław': 'PL',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    # Supplying an early date exposes the complete published archive (currently
    # beginning in 2017), while Drupal's page parameter traverses its infinite
    # scroll view without executing JavaScript.
    urls = set()
    page = 0
    while True:
        soup = get_soup(session, CONCERTS_URL, {'date': '2000-01-01', 'page': page})
        page_urls = {
            urljoin(SOURCE_URL, link.get('href'))
            for link in soup.select('article.node--type-concert a[href]')
            if urlparse(urljoin(SOURCE_URL, link.get('href'))).path.startswith('/fr/concerts/')
        }
        new_urls = page_urls - urls
        if not new_urls:
            break
        urls.update(new_urls)
        page += 1
    return sorted(urls)


def music_events(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        events.extend(
            item for item in items
            if isinstance(item, dict) and item.get('@type') in ('MusicEvent', 'Event')
        )
    return events


def resolve_location(location):
    location = location or {}
    name = clean_text(location.get('name'))
    address = location.get('address') or {}
    if isinstance(address, str):
        address = {}

    city = clean_text(address.get('addressLocality'))
    venue = name
    if not city and ',' in name:
        city, venue = (clean_text(part) for part in name.split(',', 1))
    if not city or not venue:
        return None

    country = clean_text(address.get('addressCountry')).upper()
    if len(country) != 2:
        country = CITY_COUNTRIES.get(city.casefold(), '')
    if not country:
        return None
    return venue, city, country


def detail_description(soup, event):
    parts = []
    body = soup.select_one('article.node--type-concert .concert-content-wrapper')
    if body:
        parts.append(clean_text(body))
    programme = soup.select_one(
        'article.node--type-concert .field--name-field-description '
        '.news-single-event-program-box'
    )
    if not programme:
        programme = soup.select_one(
            'article.node--type-concert .concert-sub-content '
            '.field--name-field-description'
        )
    if programme:
        programme_text = clean_text(programme)
        if programme_text:
            parts.append(f'Programme\n{programme_text}')
    fallback = clean_text(event.get('description'))
    description = clean_text('\n\n'.join(part for part in parts if part))
    return description or fallback or None


def parse_start(value):
    if not value:
        return None
    try:
        starts_at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        event_date = date(starts_at.year, starts_at.month, starts_at.day).isoformat()
    except (TypeError, ValueError):
        return None
    return event_date, starts_at.strftime('%H:%M')


def html_starts(soup):
    starts = []
    for item in soup.select(
        'article.node--type-concert .field--name-field-date > .field__items > .field__item'
    ):
        date_node = item.select_one('.concert-date time[datetime]')
        if not date_node:
            continue
        try:
            event_date = date.fromisoformat(date_node.get('datetime')[:10]).isoformat()
        except (TypeError, ValueError):
            continue
        times = item.select('.concert-hours time[datetime]')
        if not times:
            starts.append((event_date, None))
        for time_node in times:
            parsed = parse_start(time_node.get('datetime'))
            if parsed:
                starts.append((event_date, parsed[1]))
    return list(dict.fromkeys(starts))


def html_location(soup):
    node = soup.select_one(
        'article.node--type-concert .field--name-field-lieu .field__item'
    )
    return resolve_location({'name': clean_text(node)}) if node else None


def make_records(url, soup):
    records = []
    page_title = clean_text(soup.select_one('article.node--type-concert h1'))
    events = music_events(soup) or [{}]
    for event in events:
        title = page_title or clean_text(event.get('name'))
        starts = html_starts(soup)
        if not starts:
            schema_start = parse_start(event.get('startDate'))
            starts = [schema_start] if schema_start else []
        location = resolve_location(event.get('location')) or html_location(soup)
        event_url = urljoin(SOURCE_URL, event.get('url') or url)
        if not title or not starts or not location or not event_url:
            continue
        venue, city, country_code = location
        for start in starts:
            records.append({
                'title': title,
                'date': start[0],
                'url': event_url,
                'time_from': start[1],
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': detail_description(soup, event),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(make_records(url, future.result()))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
    )


class OprlBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oprl_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OprlBeCrawler().run()


if __name__ == '__main__':
    main()
