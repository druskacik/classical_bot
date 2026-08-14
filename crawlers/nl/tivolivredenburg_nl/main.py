import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tivolivredenburg.nl/'
REQUEST_BASE_URL = 'https://tivolivredenburg.nl/'
SOURCE = 'TivoliVredenburg'
AGENDA_PATH = 'agenda/'
GENRES = 'klassiek,neoklassiek,familie'
DEFAULT_CITY = 'Utrecht'

HEADERS = {
    # The www host presents Cloudflare's challenge to generic HTTP clients.
    # The canonical bare host serves the same first-party pages to crawlers.
    'User-Agent': 'Googlebot',
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

INTERNAL_VENUES = {
    'cloud nine', 'club nine', 'gehele gebouw', 'grote zaal', 'hertz',
    'pandora', 'plein 5', 'rabo open stage', 'ronda', 'tivolivredenburg',
}


def clean_text(value, separator='\n'):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def agenda_links(session):
    links = set()
    page = 1
    while True:
        url = urljoin(REQUEST_BASE_URL, AGENDA_PATH)
        if page > 1:
            url = urljoin(url, f'page/{page}/')
        soup = get_soup(session, url, params={'sf_genre': GENRES})
        page_links = {
            urljoin(SOURCE_URL, anchor.get('href'))
            for anchor in soup.select('a.agenda-list-item__title-link[href]')
        }
        page_links = {
            url for url in page_links
            if re.fullmatch(r'/agenda/\d+/[^/]+', urlparse(url).path.rstrip('/'))
        }
        new_links = page_links - links
        if not new_links:
            break
        links.update(new_links)
        if not soup.select_one('link[rel="next"]'):
            break
        page += 1
    return sorted(links)


def schema_event(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get('@type') in ('Event', 'MusicEvent'):
            return payload
    return None


def detail_values(soup):
    values = {}
    for term in soup.select('dl.description-list > dt'):
        label = clean_text(term, ' ')
        # The time rows render their value before the corresponding dt,
        # while the other definition-list rows use the normal dt/dd order.
        if label in ('Deuren open', 'Aanvang', 'Start', 'Verwachte eindtijd'):
            detail = term.find_previous_sibling('dd')
        else:
            detail = term.find_next_sibling('dd')
        if label and detail and label not in values:
            values[label] = clean_text(detail)
    return values


def resolve_location(venue):
    venue = clean_text(venue, ' ')
    if not venue or venue.lower() == 'buitenwereld':
        return None, None
    normalized = venue.lower()
    if normalized in INTERNAL_VENUES:
        return venue, DEFAULT_CITY
    # The agenda includes off-site performances. Only infer their city when
    # the first-party venue label itself makes it explicit.
    if re.search(r'\bzeist\b', normalized):
        return venue, 'Zeist'
    return venue, DEFAULT_CITY


def description_text(soup, event, values):
    parts = []
    for label in ('Uitvoerenden', 'Programma'):
        text = values.get(label)
        if text:
            parts.append(f'{label}\n{text}')
    body = clean_text(event.get('description'))
    if body:
        parts.append(body)
    unique = []
    for part in parts:
        if part not in unique:
            unique.append(part)
    return '\n\n'.join(unique) or None


def make_record(soup, url):
    event = schema_event(soup)
    if not event:
        return None
    try:
        start = datetime.fromisoformat(str(event.get('startDate', '')).replace('Z', '+00:00'))
    except ValueError:
        return None

    values = detail_values(soup)
    venue, city = resolve_location(values.get('Locatie'))
    title = clean_text(event.get('name'), ' ')
    if not title or not venue or not city:
        return None

    time_text = values.get('Aanvang') or values.get('Start')
    match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', time_text or '')
    time_from = match.group(0) if match else start.strftime('%H:%M')
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'NL',
        'description': description_text(soup, event, values),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    request_url = url.replace('https://www.tivolivredenburg.nl/', REQUEST_BASE_URL)
    return make_record(get_soup(session, request_url), url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = agenda_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_detail, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
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
        key=lambda record: (record['date'], record['time_from'], record['title'], record['url']),
    )


class TivoliVredenburgNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tivolivredenburg_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
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
    TivoliVredenburgNlCrawler().run()


if __name__ == '__main__':
    main()
