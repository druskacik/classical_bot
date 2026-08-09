import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatromayor.org/'
SEASON_URL = urljoin(SOURCE_URL, 'es/temporada/')
SOURCE = 'Teatro Mayor Julio Mario Santo Domingo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'es-CO,es;q=0.9,en;q=0.6',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(session, url):
    error = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise error


def canonical_event_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def listing_urls(session):
    url = f'{SEASON_URL}{date.today().year}'
    seen_pages = set()
    events = set()

    while url and url not in seen_pages:
        seen_pages.add(url)
        soup = BeautifulSoup(get_html(session, url), 'html.parser')
        for link in soup.select('a[href*="/evento/"]'):
            href = link.get('href')
            if href:
                events.add(canonical_event_url(href))

        next_link = soup.select_one(
            '.pager-next a, .pager--infinite-scroll a, a[title="Ir a la página siguiente"]'
        )
        url = urljoin(url, next_link['href']) if next_link and next_link.get('href') else None

    return sorted(events)


def drupal_functions(soup):
    decoder = json.JSONDecoder()
    for script in soup.find_all('script'):
        text = script.string or script.get_text()
        marker = '"functions":'
        position = text.find(marker)
        if position == -1:
            continue
        try:
            value, _ = decoder.raw_decode(text[position + len(marker):].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    return []


def resolve_city(address):
    address = clean_text(address)
    if re.search(r'\bbogot[aá]\b', address, re.IGNORECASE):
        return 'Bogotá'

    # Touring/partner stages occasionally appear in this calendar. Only use a
    # final address component when it looks like a city name, not an address.
    final_part = address.rsplit(',', 1)[-1].strip()
    if re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]{2,60}", final_part):
        return final_part
    return None


def make_records(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('h1.title')
    fallback_title = clean_text(heading.get_text(' ', strip=True) if heading else '')
    body = soup.select_one('.field-name-eventbody')
    description = clean_text(body) or None
    records = []

    for function in drupal_functions(soup):
        formatted = function.get('format_date') or {}
        title = clean_text(function.get('event_title') or function.get('title')) or fallback_title
        venue = clean_text(function.get('stage_name'))
        city = resolve_city(function.get('stage_address'))
        event_url = function.get('url') or f'{url}?function={function.get("id", "")}'
        try:
            event_date = date(
                int(formatted.get('year')),
                int(formatted.get('month')),
                int(formatted.get('day')),
            ).isoformat()
        except (TypeError, ValueError):
            continue

        time_match = re.fullmatch(r'(\d{1,2}):(\d{2})\s*([AP]M)', formatted.get('time', ''), re.I)
        time_from = None
        if time_match:
            hour = int(time_match.group(1)) % 12
            if time_match.group(3).upper() == 'PM':
                hour += 12
            time_from = f'{hour:02d}:{time_match.group(2)}'

        if not title or not venue or not city or not event_url:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': urljoin(SOURCE_URL, event_url),
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'CO',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_event(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return make_records(url, get_html(session, url))


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(scrape_event, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
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
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class TeatroMayorOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatromayor_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CO',
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
        return get_concerts()


def main():
    TeatroMayorOrgCrawler().run()


if __name__ == '__main__':
    main()
