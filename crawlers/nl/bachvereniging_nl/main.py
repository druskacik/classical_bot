import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bachvereniging.nl/'
AGENDA_URL = urljoin(SOURCE_URL, 'concerten-agenda')
ARCHIVE_URL = urljoin(SOURCE_URL, 'concerten-archief')
SOURCE = 'Nederlandse Bachvereniging'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'januari': 1, 'februari': 2, 'maart': 3, 'april': 4,
    'mei': 5, 'juni': 6, 'juli': 7, 'augustus': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'december': 12,
    'jan': 1, 'feb': 2, 'mrt': 3, 'apr': 4, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def production_links(session):
    links = set()
    for index_url in (AGENDA_URL, ARCHIVE_URL):
        soup = get_soup(session, index_url)
        for anchor in soup.select('a.event-title[href], a.production-card-link[href]'):
            url = urljoin(SOURCE_URL, anchor.get('href'))
            if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
                links.add(url)
    return sorted(links)


def parse_datetime(value):
    text = clean_text(value).lower()
    match = re.search(
        r'\b(\d{1,2})\s+([a-z]+)\s+(\d{4})(?:\s*-\s*(\d{1,2})[:.]([0-5]\d))?',
        text,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2))
    if not month:
        return None, None
    try:
        parsed = datetime(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None, None
    time_from = f'{int(match.group(4)):02d}:{match.group(5)}' if match.group(4) else None
    return parsed.date().isoformat(), time_from


def parse_location(value):
    parts = [part.strip() for part in clean_text(value).split(',') if part.strip()]
    if len(parts) < 2:
        return None, None, None
    country_code = 'NL'
    country_names = {'duitsland': 'DE', 'spanje': 'ES', 'zwitserland': 'CH'}
    if parts[0].casefold() in country_names:
        country_code = country_names[parts.pop(0).casefold()]
    if len(parts) < 2 or 'verschillende locaties' in ' '.join(parts).casefold():
        return None, None, None

    # One historical CMS entry reverses its venue and city.
    if parts[0] == 'Parkstad Limburg Theaters' and parts[1] == 'Heerlen':
        parts[0], parts[1] = parts[1], parts[0]
    city = parts[0]
    if city == 'Hasselt':
        country_code = 'BE'
    venue_parts = parts[1:]
    if venue_parts and venue_parts[0].casefold() in ('belgie', 'belgië'):
        country_code = 'BE'
        venue_parts = venue_parts[1:]
    # A few CMS records repeat the city before the actual hall name.
    if len(venue_parts) > 1 and venue_parts[0].casefold() == city.casefold():
        venue_parts = venue_parts[1:]
    venue = ', '.join(venue_parts)
    return venue or None, city or None, country_code


def detail_description(soup):
    parts = []
    selectors = (
        '.production-introduction',
        '.production-body-margin',
        '.production-concert-section',
    )
    for selector in selectors:
        for node in soup.select(selector):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)

    # Current templates put the repertoire and performers in an unlabelled
    # grid immediately below the "Werken en Uitvoering" heading.
    heading = next(
        (node for node in soup.select('h2') if clean_text(node).casefold() == 'werken en uitvoering'),
        None,
    )
    if heading:
        container = heading.find_parent('section') or heading.parent
        text = clean_text(container)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail(session, url):
    soup = get_soup(session, url)
    description = detail_description(soup)
    production_title = clean_text(
        soup.select_one('h1.production-title, .production-header-title')
    )
    records = []
    for event in soup.select('article.event'):
        title_node = event.select_one('a.event-title')
        date_node = event.select_one('time')
        location_node = event.select_one(
            '.event-location, .event-title-and-date h4'
        )
        title = clean_text(title_node) or production_title
        date, time_from = parse_datetime(date_node)
        venue, city, country_code = parse_location(location_node)
        if not title or not date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': date,
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


class BachverenigingNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachvereniging_nl',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        links = production_links(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(parse_detail, session, url): url for url in links}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Bachvereniging production',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    BachverenigingNlCrawler().run()


if __name__ == '__main__':
    main()
