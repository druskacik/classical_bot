import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tog.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'spielplan-programm/')
SOURCE = 'Theater und Orchester Neubrandenburg/Neustrelitz'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# Touring dates use "unterwegs" in the normal venue position and put the
# actual venue in the following column. These markers cover the named venues
# in the published season and allow a city without treating "unterwegs" as one.
CITY_MARKERS = {
    'Altentreptow': 'Altentreptow',
    'Berlin-Reinickendorf': 'Berlin',
    'Bollewick': 'Bollewick',
    'Domjüch': 'Neustrelitz',
    'Elbphilharmonie Hamburg': 'Hamburg',
    'Fleesensee': 'Göhren-Lebbin',
    'Güstrow': 'Güstrow',
    'Kevelaer': 'Kevelaer',
    'Malchin': 'Malchin',
    'Mirow': 'Mirow',
    'Mönchengladbach': 'Mönchengladbach',
    'Neubrandenburg': 'Neubrandenburg',
    'Neustrelitz': 'Neustrelitz',
    'Rostock': 'Rostock',
    'Waren': 'Waren (Müritz)',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=8,
        pool_maxsize=8,
        max_retries=Retry(
            total=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    # A harmless unique query avoids stale Cloudflare challenge responses while
    # the canonical URL remains the one stored on each record.
    separator = '&' if '?' in url else '?'
    last_error = None
    for attempt in range(3):
        request_url = f'{url}{separator}crawler={time.time_ns()}-{attempt}'
        try:
            response = session.get(request_url, timeout=45)
            response.raise_for_status()
            if 'Just a moment' in response.text[:1000]:
                raise requests.RequestException(
                    'Cloudflare challenge returned instead of page HTML'
                )
            return BeautifulSoup(response.text, 'html.parser')
        except requests.RequestException as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise last_error


def infer_city(venue):
    for marker, city in CITY_MARKERS.items():
        if marker.casefold() in venue.casefold():
            return city
    return None


def parse_card(card):
    container = card.select_one('.tickets-container')
    columns = container.find_all('div', recursive=False) if container else []
    if len(columns) < 3:
        return None

    date_node = columns[0].select_one('p')
    location_parts = columns[1].find_all('p', recursive=False)
    link = columns[2].select_one('p.fs25 a[href]')
    if not date_node or len(location_parts) < 2 or not link:
        return None

    try:
        date = datetime.strptime(clean_text(date_node), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None

    time_text = clean_text(location_parts[0])
    time_from = time_text if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_text) else None
    venue = clean_text(location_parts[1]).replace('\n', ', ')
    detail_paragraphs = columns[2].find_all('p', recursive=False)
    if venue.casefold() == 'unterwegs':
        venue = clean_text(detail_paragraphs[0]).replace('\n', ', ') if detail_paragraphs else ''

    city = infer_city(venue)
    title = clean_text(link)
    if not title or not venue or not city:
        return None

    summaries = []
    for paragraph in detail_paragraphs:
        if paragraph.select_one('a[href]'):
            continue
        value = clean_text(paragraph)
        if value and value.casefold() not in {'premiere', 'doppelpremiere'}:
            summaries.append(value)

    return {
        'title': title,
        'date': date,
        'url': canonical_url(urljoin(CALENDAR_URL, link['href'])),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': '\n'.join(dict.fromkeys(summaries)) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, record):
    soup = get_soup(session, record['url'])
    detail = max((clean_text(body) for body in soup.select('.page_projekte')), default='', key=len)
    if detail:
        summary = record.get('description')
        record['description'] = detail if not summary or summary in detail else f'{summary}\n\n{detail}'
    return record


def get_concerts():
    session = make_session()
    soup = get_soup(session, CALENDAR_URL)
    records = [
        record for card in soup.select('.spielplan_bloc')
        if (record := parse_card(card))
    ]

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    records = list(unique.values())

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_description, session, record): record for record in records}
        enriched = []
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape TOG event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                enriched.append(record)

    return sorted(enriched, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url']
    ))


class TogDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tog_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TogDeCrawler().run()


if __name__ == '__main__':
    main()
