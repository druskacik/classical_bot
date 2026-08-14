import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.spotgroningen.nl/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programma/')
SOURCE = 'SPOT Groningen'
DEFAULT_CITY = 'Groningen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
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


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def candidate_links(session):
    soup = fetch_soup(session, PROGRAMME_URL)
    links = set()
    for item in soup.select('main article.program__item'):
        genres = set((item.get('data-genres') or '').split(','))
        subgenres = set((item.get('data-subgenres') or '').split(','))
        if 'klassiek' not in genres and not subgenres.intersection({'ballet', 'jeugd-klassiek'}):
            continue
        anchor = item.select_one('a[href]')
        if not anchor:
            continue
        url = urljoin(SOURCE_URL, anchor.get('href'))
        # Festival landing pages group multiple occurrences and are not concerts.
        if urlparse(url).path.startswith('/programma/'):
            links.add(url)
    return sorted(links)


def schema_event(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            payload = json.loads(re.sub(r',\s*([}\]])', r'\1', raw))
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def event_title(soup, event):
    header = soup.select_one('.content__article > header.event__header')
    if header:
        parts = [clean_text(node) for node in header.select('h1, h2')]
        title = ' | '.join(part for part in parts if part)
        if title:
            return title
    return clean_text(event.get('name'))


def event_description(soup, event):
    parts = []
    summary = clean_text(event.get('description'))
    if summary:
        parts.append(summary)
    section = soup.select_one('.content__article > section.event__language--is-active')
    body = clean_text(section, separator='\n')
    if body and body not in parts:
        parts.append(body)
    return '\n\n'.join(parts) or None


def event_venue(soup, event):
    def without_address(value):
        value = re.split(r'\s+/\s+(?=[^/]*\d)', value, maxsplit=1)[0]
        value = re.sub(r',\s*[^,]*\d+[A-Za-z]?(?:\s*,.*)?$', '', value)
        value = clean_text(value).rstrip(' ,')
        if re.fullmatch(r'Trompsingel\s+27', value, flags=re.I):
            return 'SPOT/De Oosterpoort'
        if re.fullmatch(r'Turfsingel\s+86', value, flags=re.I):
            return 'SPOT/Stadsschouwburg'
        return value

    timetable = soup.select_one('.content__article .event__timetable.event__language--is-active')
    if timetable:
        lines = [clean_text(line) for line in timetable.stripped_strings]
        location_line = next(
            (line for line in lines if line.startswith(('SPOT/', 'Lutherse Kerk', 'USVA', 'A-Theater'))),
            '',
        )
        if location_line:
            venue = without_address(location_line)
            if venue:
                return venue

    location = event.get('location') or {}
    address = clean_text(location.get('address')) if isinstance(location, dict) else ''
    if address:
        venue = without_address(address)
        if re.match(r'^Trompsingel\s+27(?:,|$)', address, flags=re.I):
            venue = 'SPOT/De Oosterpoort'
        elif re.match(r'^Turfsingel\s+86(?:,|$)', address, flags=re.I):
            venue = 'SPOT/Stadsschouwburg'
        return venue or None
    return None


def make_record(soup, event, url):
    title = event_title(soup, event)
    start = event.get('startDate')
    try:
        parsed = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    venue = event_venue(soup, event)
    if not title or not venue:
        return None
    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': url,
        'time_from': parsed.strftime('%H:%M'),
        'venue': venue,
        'city': DEFAULT_CITY,
        'country_code': 'NL',
        'description': event_description(soup, event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(session, url):
    soup = fetch_soup(session, url)
    event = schema_event(soup)
    return make_record(soup, event, url) if event else None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = candidate_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scrape_detail, session, url): url for url in links}
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
    return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title'], row['url']))


class SpotGroningenNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='spotgroningen_nl',
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
    SpotGroningenNlCrawler().run()


if __name__ == '__main__':
    main()
