import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cappellaamsterdam.nl/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap_index.xml')
SOURCE = 'Cappella Amsterdam'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1,
    'feb': 2,
    'mrt': 3,
    'apr': 4,
    'mei': 5,
    'jun': 6,
    'jul': 7,
    'aug': 8,
    'sep': 9,
    'okt': 10,
    'nov': 11,
    'dec': 12,
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def fetch(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def sitemap_locations(xml_text):
    root = ElementTree.fromstring(xml_text)
    return [node.text.strip() for node in root.findall('.//{*}loc') if node.text]


def production_urls(session):
    sitemap_urls = [
        url
        for url in sitemap_locations(fetch(session, SITEMAP_URL).text)
        if re.search(r'/productie-sitemap\d*\.xml$', url)
    ]
    urls = set()
    for sitemap_url in sitemap_urls:
        urls.update(
            url
            for url in sitemap_locations(fetch(session, sitemap_url).text)
            if '/productie/' in url
        )
    return sorted(urls)


def parse_date(value):
    match = re.fullmatch(r'\d{1,2}\s+([a-z]{3})\s+(\d{4})', value.lower())
    if not match or match.group(1) not in MONTHS:
        return None
    day = int(value.split()[0])
    try:
        return datetime(int(match.group(2)), MONTHS[match.group(1)], day).date().isoformat()
    except ValueError:
        return None


def parse_location(event):
    city = clean_text(event.select_one('.event-location .city'))
    venue = clean_text(event.select_one('.event-location .venue'))
    if not city or not venue:
        return None, None, None

    country_code = 'NL'
    suffix = re.search(r'\(([A-Z]{2})\)\s*$', city)
    if suffix:
        # The source uses the non-ISO abbreviation "SU" for Switzerland.
        country_code = {'SU': 'CH'}.get(suffix.group(1), suffix.group(1))
        city = city[:suffix.start()].strip()
    else:
        ticket = event.select_one('a.event-ticket-link[href]')
        hostname = urlparse(ticket.get('href')).hostname if ticket else None
        country_code = {
            'at': 'AT', 'be': 'BE', 'ch': 'CH', 'de': 'DE',
            'es': 'ES', 'fr': 'FR',
        }.get((hostname or '').rsplit('.', 1)[-1], country_code)
    return city or None, venue or None, country_code


def detail_description(soup):
    parts = []
    for heading in soup.select('h3.block-title-aside'):
        label = clean_text(heading).lower()
        if label not in ('programma', 'achtergrond'):
            continue
        row = heading.find_parent(class_='row')
        if not row:
            continue
        columns = row.find_all(class_='columns', recursive=False)
        if len(columns) < 2:
            continue
        text = clean_text(columns[1], separator='\n')
        if text:
            parts.append(f'{label.capitalize()}\n{text}')
    return '\n\n'.join(parts) or None


def scrape_production(session, url):
    soup = BeautifulSoup(fetch(session, url).text, 'html.parser')
    title = clean_text(soup.select_one('h1.intro-title'))
    if not title:
        return []
    description = detail_description(soup)
    records = []
    for event in soup.select('.production-event--single'):
        date = parse_date(clean_text(event.select_one('.event-date-time .date')))
        city, venue, country_code = parse_location(event)
        if not date or not city or not venue:
            continue
        time_text = clean_text(event.select_one('.event-date-time .time'))
        time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', time_text)
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_match.group(0).zfill(5) if time_match else None,
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
    urls = production_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scrape_production, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ElementTree.ParseError, ValueError) as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'],
            record['city'], record['venue'],
        ),
    )


class CappellaAmsterdamNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cappellaamsterdam_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    CappellaAmsterdamNlCrawler().run()


if __name__ == '__main__':
    main()
