import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theatrodapaz.com.br/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programa%C3%A7%C3%A3o')
SITEMAP_URL = urljoin(SOURCE_URL, 'event-pages-sitemap.xml')
SOURCE = 'Theatro da Paz'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for location in soup.find_all('loc'):
        url = location.get_text(strip=True)
        if '/informa-es-do-evento-e-registro/' in url:
            urls.append(url.split('?', 1)[0])
    if urls:
        return list(dict.fromkeys(urls))

    # Keep the public schedule as a fallback if Wix temporarily omits the
    # event sitemap. Unlike the schedule, the sitemap also retains archives.
    soup = get_soup(session, PROGRAM_URL)
    for link in soup.select('a[data-anchor="event-details"][href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        if '/informa-es-do-evento-e-registro/' in url:
            urls.append(url.split('?', 1)[0])
    return list(dict.fromkeys(urls))


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def description_from_page(soup, schema):
    parts = []
    summary = clean_text(schema.get('description'))
    if summary:
        parts.append(summary)

    about = soup.select_one('[data-hook="about-section"]')
    if about:
        for node in about.select('[data-hook="about"], [data-hook="about-section-button"]'):
            node.decompose()
        body = clean_text(about.get_text('\n', strip=True))
        if body and body not in parts:
            parts.append(body)
    return '\n\n'.join(parts) or None


def city_from_location(location):
    address = location.get('address') or ''
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
        country = clean_text(address.get('addressCountry'))
        if city and (not country or country.upper() in ('BR', 'BRAZIL', 'BRASIL')):
            return city
        address = ' '.join(clean_text(value) for value in address.values())
    address = clean_text(address)
    match = re.search(r'\bBel[eé]m\s*-\s*PA\b', address, re.IGNORECASE)
    return 'Belém' if match else None


def venue_from_location(location):
    venue = clean_text(location.get('name'))
    address = location.get('address') or ''
    if isinstance(address, dict):
        address = ' '.join(clean_text(value) for value in address.values())
    address = clean_text(address).casefold()
    # Older Wix records sometimes put the neighbourhood (Campina) or city in
    # the place-name field. This full address is the theatre's fixed address.
    if (
        'avenida da paz' in address
        and 'praça da república' in address
        and '66017-060' in address
    ):
        return SOURCE
    return venue


def parse_event(session, url):
    soup = get_soup(session, url)
    schema = event_schema(soup)
    if not schema:
        return None

    title = clean_text(schema.get('name'))
    start = clean_text(schema.get('startDate'))
    location = schema.get('location') or {}
    venue = venue_from_location(location)
    city = city_from_location(location)
    match = re.match(r'(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})', start)
    if (
        not title
        or not match
        or not venue
        or not city
        or venue.casefold() == city.casefold()
    ):
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'description': description_from_page(soup, schema),
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_event, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
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
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class TheatroDaPazComBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theatrodapaz_com_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheatroDaPazComBrCrawler().run()


if __name__ == '__main__':
    main()
