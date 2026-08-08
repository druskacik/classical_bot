import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.antwerpsymphonyorchestra.be/'
AGENDA_URL = urljoin(SOURCE_URL, 'nl/programma')
SOURCE = 'Antwerp Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-BE,nl;q=0.9,en;q=0.7',
}

# The orchestra tours, so the location on each performance is authoritative.
# These cover the cities occurring regularly in its published calendar.
CITY_COUNTRIES = {
    'antwerpen': 'BE',
    'brussel': 'BE',
    'bruxelles': 'BE',
    'brugge': 'BE',
    'gent': 'BE',
    'hasselt': 'BE',
    'mechelen': 'BE',
    'turnhout': 'BE',
    'amsterdam': 'NL',
    'eindhoven': 'NL',
    'rotterdam': 'NL',
    'den haag': 'NL',
    'parijs': 'FR',
    'paris': 'FR',
    'keulen': 'DE',
    'köln': 'DE',
    'londen': 'GB',
    'london': 'GB',
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
    # Explicit dates expose the site's archive as well as its future season.
    # The archive currently begins long after 2000; the early bound keeps this
    # universal if older productions are subsequently republished.
    params = {'start': '2000-01-01', 'end': '2100-12-31', 'page': 1}
    first = get_soup(session, AGENDA_URL, params=params)
    pages = [int(option.get('value')) for option in first.select('select[name="page"] option')]
    last_page = max(pages, default=1)

    urls = set()

    def add_urls(soup):
        for link in soup.select('a.image[href*="/programma/"]'):
            url = urljoin(SOURCE_URL, link.get('href'))
            if urlparse(url).path.startswith('/nl/programma/'):
                urls.add(url)

    add_urls(first)
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(
                get_soup,
                session,
                AGENDA_URL,
                {'start': params['start'], 'end': params['end'], 'page': page},
            ): page
            for page in range(2, last_page + 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                add_urls(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape agenda page',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{AGENDA_URL}?page={page}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(urls)


def event_objects(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        events.extend(item for item in items if isinstance(item, dict) and item.get('@type') == 'Event')
    return events


def resolve_location(location):
    name = clean_text((location or {}).get('name'))
    if not name:
        return None
    parts = [part.strip() for part in name.rsplit(',', 1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        venue, city = parts
    else:
        venue = name
        lowered = name.lower()
        city = next((candidate.title() for candidate in CITY_COUNTRIES if candidate in lowered), '')
        if not city and any(term in lowered for term in ('elisabethzaal', 'elckerlyc', 'de roma')):
            city = 'Antwerpen'
    country = CITY_COUNTRIES.get(city.lower())
    if not venue or not city or not country:
        return None
    return venue, city, country


def detail_description(soup, event):
    parts = []
    # The first rich-text block is the editorial description. ProgrammeWrapper
    # contains the full composer/work listing, which is needed downstream.
    main = soup.select_one('main')
    if main:
        richtext = main.select_one('.richtext')
        if richtext:
            parts.append(clean_text(richtext))
        programme = main.select_one('.programmeWrapper')
        if programme:
            parts.append(clean_text(programme))
    fallback = clean_text(event.get('description'))
    description = clean_text('\n\n'.join(part for part in parts if part))
    return description or fallback or None


def make_records(url, soup):
    page_title = clean_text(soup.select_one('main h1'))
    records = []
    for event in event_objects(soup):
        title = page_title or clean_text(event.get('name'))
        location = resolve_location(event.get('location'))
        start = event.get('startDate')
        if not title or not location or not start:
            continue
        try:
            starts_at = datetime.fromisoformat(start.replace('Z', '+00:00'))
            event_date = date(starts_at.year, starts_at.month, starts_at.day).isoformat()
        except (TypeError, ValueError):
            continue
        venue, city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': starts_at.strftime('%H:%M'),
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
    return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title'], row['venue']))


class AntwerpSymphonyOrchestraBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='antwerpsymphonyorchestra_be',
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
    AntwerpSymphonyOrchestraBeCrawler().run()


if __name__ == '__main__':
    main()
