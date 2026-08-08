import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.symfonieorkest.be/'
CONCERTS_URL = urljoin(SOURCE_URL, 'nl/concerten')
ARCHIVE_URL = urljoin(SOURCE_URL, 'nl/concerten/archief')
SOURCE = 'Symfonieorkest Vlaanderen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-BE,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'feb': 2, 'mrt': 3, 'mar': 3, 'apr': 4, 'mei': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'sept': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}

# The orchestra tours. Locations on individual performance rows are therefore
# authoritative, rather than the organisation's home address in Ghent.
CITY_COUNTRIES = {
    'aalst': 'BE', 'antwerpen': 'BE', 'brugge': 'BE', 'brussel': 'BE',
    'bruxelles': 'BE', 'gent': 'BE', 'hasselt': 'BE', 'kortrijk': 'BE',
    'leuven': 'BE', 'mechelen': 'BE', 'oostende': 'BE', 'roeselare': 'BE',
    'sint-niklaas': 'BE', 'turnhout': 'BE', 'waregem': 'BE',
    'amsterdam': 'NL', 'den haag': 'NL', 'eindhoven': 'NL', 'maastricht': 'NL',
    'rotterdam': 'NL', 'utrecht': 'NL', 'hilversum': 'NL',
    'parijs': 'FR', 'paris': 'FR', 'lille': 'FR',
    'keulen': 'DE', 'köln': 'DE', 'berlijn': 'DE', 'berlin': 'DE',
    'londen': 'GB', 'london': 'GB', 'luxemburg': 'LU', 'luxembourg': 'LU',
}

VENUE_CITIES = {
    'concertgebouw brugge': 'Brugge',
    'muziekcentrum de bijloke': 'Gent',
    'de bijloke': 'Gent',
    'stadshal': 'Gent',
    'de singel': 'Antwerpen',
    'koningin elisabethzaal': 'Antwerpen',
    'ccha': 'Hasselt',
    'bozar': 'Brussel',
    'concertgebouw amsterdam': 'Amsterdam',
    'muziekgebouw aan ’t ij': 'Amsterdam',
    "muziekgebouw aan 't ij": 'Amsterdam',
    'tivolivredenburg': 'Utrecht',
    'de doelen': 'Rotterdam',
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
    current = get_soup(session, CONCERTS_URL)
    archive = get_soup(session, ARCHIVE_URL)
    season_ids = {
        field.get('value') for field in archive.select('input[name="season"][value]')
        if field.get('value')
    }
    soups = [current, archive]
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(get_soup, session, ARCHIVE_URL, {'season': season_id})
            for season_id in season_ids
        ]
        for future in as_completed(futures):
            try:
                soups.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert archive season',
                    event='crawler_page_failed',
                    level='warning',
                    url=ARCHIVE_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    urls = set()
    for soup in soups:
        for link in soup.select('a[href*="/nl/concerten/"]'):
            url = urljoin(SOURCE_URL, link.get('href'))
            path = urlparse(url).path.rstrip('/')
            if path != '/nl/concerten/archief':
                urls.add(url)
    return sorted(urls)


def parse_datetime(value):
    match = re.search(
        r'(?P<day>\d{1,2})\s+(?P<month>[a-z]+)\.?\s+(?P<year>\d{4})'
        r'(?:\s+(?P<time>\d{1,2}:\d{2}))?',
        clean_text(value).lower().replace('\n', ' '),
    )
    if not match:
        return None
    month = MONTHS.get(match.group('month').rstrip('.'))
    if not month:
        return None
    try:
        concert_date = datetime(
            int(match.group('year')), month, int(match.group('day'))
        ).date().isoformat()
    except ValueError:
        return None
    return concert_date, match.group('time')


def resolve_location(value):
    location = clean_text(value)
    if not location or location.lower() == 'meerdere locaties':
        return None

    # Location pills use either "venue, city" or a self-identifying venue name.
    parts = [part.strip() for part in location.rsplit(',', 1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        venue, city = parts
    else:
        venue = location
        lowered = location.lower()
        city = next(
            (known_city for key, known_city in VENUE_CITIES.items() if key in lowered),
            '',
        )
        if not city:
            city = next(
                (candidate.title() for candidate in CITY_COUNTRIES if candidate in lowered),
                '',
            )
    country_code = CITY_COUNTRIES.get(city.lower())
    if not venue or not city or not country_code:
        return None
    return venue, city, country_code


def description_from_page(soup):
    # Rich-text regions include the full programme/composer list and editorial
    # notes. Performer credits are harmless context for the downstream parser.
    parts = [
        clean_text(block) for block in soup.select('.richtext')
        if not block.find_parent('footer')
    ]
    parts = [part for part in parts if part]
    return clean_text('\n\n'.join(dict.fromkeys(parts))) or None


def make_records(url, soup):
    title = clean_text(soup.select_one('h1'))
    if not title:
        return []
    description = description_from_page(soup)
    records = []
    for date_node in soup.select('.t-date'):
        parsed = parse_datetime(date_node.get_text(' ', strip=True))
        if not parsed:
            continue
        # Templates changed across the nine published archive seasons. Find
        # the closest enclosing performance element that also contains its
        # location, instead of depending on a fixed nesting depth.
        card = date_node
        pill = None
        for _ in range(6):
            if card.parent is None:
                break
            card = card.parent
            pill = card.select_one('.pill')
            if pill:
                break
        location = resolve_location(pill)
        if not location:
            continue
        event_date, time_from = parsed
        venue, city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
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


class SymfonieorkestBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symfonieorkest_be',
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
    SymfonieorkestBeCrawler().run()


if __name__ == '__main__':
    main()
