import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.konzerthaus.de/de/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programm/')
SOURCE = 'Konzerthaus Berlin'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

COUNTRIES = {
    'deutschland': 'DE',
    'germany': 'DE',
    'schweiz': 'CH',
    'switzerland': 'CH',
    'österreich': 'AT',
    'austria': 'AT',
}


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    text = (
        BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
        if '<' in value and '>' in value
        else value
    )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def detail_links(session):
    # Dates earlier than the first day of the current month are normalised by
    # the site to the same first page. Starting here captures the past events
    # which remain in the public calendar without repeatedly fetching them.
    today = date.today()
    url = f'{PROGRAM_URL}01-{today.month:02d}-{today.year}'
    links = []
    seen_pages = set()

    while url and url not in seen_pages:
        seen_pages.add(url)
        soup = get_page(session, url)
        for item in soup.select('li.event-item'):
            link = item.select_one('h2.event-title a[href*="/programm/"]')
            if not link:
                continue
            href = urljoin(SOURCE_URL, link.get('href', ''))
            if re.search(r'/programm/[^/]+/\d+/?(?:\?|$)', href):
                links.append(href.split('?', 1)[0])

        next_link = next(
            (link for link in soup.find_all('a', href=True)
             if clean_text(link.get_text()) == 'Next'),
            None,
        )
        url = urljoin(SOURCE_URL, next_link['href']) if next_link else None

    return list(dict.fromkeys(links))


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') in {
                'Event', 'MusicEvent'
            }:
                return candidate
    return {}


def location_fields(schema):
    location = schema.get('location') or {}
    if not isinstance(location, dict):
        return None
    venue = clean_text(location.get('name'))
    address = location.get('address') or ''
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
        country_text = clean_text(address.get('addressCountry'))
        address_text = ' '.join(clean_text(value) for value in address.values())
    else:
        address_text = clean_text(address)
        country_text = address_text
        city = ''

    country_code = next(
        (code for name, code in COUNTRIES.items() if name in country_text.lower()),
        None,
    )
    if not city:
        match = re.search(r'(?:^|,\s*)\d{4,5}\s+([^,]+)', address_text)
        city = clean_text(match.group(1)) if match else ''

    # The detail schema supplies full addresses for external venues. The home
    # default is only used when no contrary location evidence is present.
    if not city and venue and not address_text:
        city, country_code = 'Berlin', 'DE'
    elif city and not country_code and city.lower() == 'berlin':
        country_code = 'DE'

    if not venue or not city or not country_code:
        return None
    return venue, city, country_code


def description_text(soup):
    container = soup.select_one('.content-inner.artists')
    if not container:
        return None
    text = clean_text(container.get_text('\n', strip=True))
    return text or None


def make_record(url, soup):
    schema = event_schema(soup)
    title = clean_text(schema.get('name'))
    start = clean_text(schema.get('startDate'))
    match = re.match(r'^(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}))?', start)
    location = location_fields(schema)
    if not title or not match or not location:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    venue, city, country_code = location
    time_from = f'{match.group(2)}:{match.group(3)}' if match.group(2) else None
    canonical = clean_text(schema.get('url')) or url
    return {
        'title': title,
        'date': event_date,
        'url': canonical,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_text(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return make_record(url, get_page(session, url))


class KonzerthausDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='konzerthaus_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = detail_links(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(scrape_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
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

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    KonzerthausDeCrawler().run()


if __name__ == '__main__':
    main()
