import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hfm-nuernberg.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'veranstaltungen/uebersicht')
SOURCE = 'Hochschule für Musik Nürnberg'
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
VENUE_CITY_MARKERS = {
    'forchheim': 'Forchheim', 'fürth': 'Fürth', 'ingolstadt': 'Ingolstadt',
    'würzburg': 'Würzburg', 'schwabach': 'Schwabach', 'schwetzingen': 'Schwetzingen',
    'hilpoltstein': 'Hilpoltstein', 'weißenburg': 'Weißenburg in Bayern',
    'münchen': 'München', 'nordheim': 'Nordheim am Main',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\xad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def month_urls(soup):
    urls = []
    for item in soup.select('.month-filter .list-group-item.has-news a[href]'):
        match = re.search(r'/veranstaltungen/uebersicht/(20\d{2})/(\d{1,2})$', item['href'])
        if not match:
            continue
        year, month = map(int, match.groups())
        try:
            date(year, month, 1)
        except ValueError:
            continue
        urls.append(urljoin(SOURCE_URL, f'veranstaltungen/uebersicht/{year}/{month}/ajax.xml?ceUid=542'))
    return sorted(set(urls))


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)', clean_text(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_card(card):
    link = card.select_one('a[href*="/veranstaltungen/termin/"]')
    title = clean_text(card.select_one('.event-title'))
    moment = card.select_one('.event-date time[datetime]')
    venue = clean_text(card.select_one('.additional__infos--location'))
    if not link or not title or not moment or not venue:
        return None
    try:
        event_date = date.fromisoformat(moment['datetime'][:10]).isoformat()
    except (KeyError, TypeError, ValueError):
        return None
    city = 'Nürnberg'
    folded_venue = venue.casefold()
    for marker, marker_city in VENUE_CITY_MARKERS.items():
        if marker in folded_venue:
            city = marker_city
            break
    return {
        'title': title.replace('\n', ' - '),
        'date': event_date,
        'url': urljoin(SOURCE_URL, link['href']),
        'time_from': parse_time(card.select_one('.event-time')),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_month(soup):
    return [record for card in soup.select('.card--event') if (record := parse_card(card))]


def enrich_record(session, record):
    soup = get_soup(session, record['url'])
    detail = soup.select_one('.news-single')
    if not detail:
        return record

    description_parts = []
    for selector in ('.teaser-text', '.news-text-wrap', '.article .frame-type-text'):
        for node in detail.select(selector):
            value = clean_text(node)
            if value and value not in description_parts:
                description_parts.append(value)
    meta = soup.select_one('meta[name="description"][content]')
    if not description_parts and meta:
        value = clean_text(meta.get('content'))
        if value:
            description_parts.append(value)
    record['description'] = '\n\n'.join(description_parts) or None

    address = clean_text(detail.select_one('.address'))
    city_match = re.search(r'\b\d{5}\s+([^,\n]+)', address)
    if city_match:
        record['city'] = city_match.group(1).strip()
    return record


def get_concerts():
    session = make_session()
    index_soup = get_soup(session, CALENDAR_URL)
    urls = month_urls(index_soup)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_month(future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape HfM Nürnberg calendar month',
                    event='crawler_page_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

    unique = {(item['url'], item['date'], item['time_from'], item['venue']): item for item in records}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(enrich_record, session, item): item for item in unique.values()}
        enriched = []
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape HfM Nürnberg event detail',
                    event='crawler_item_failed', level='warning', url=record['url'],
                    error_type=type(error).__name__, error_message=str(error),
                )
                enriched.append(record)
    return sorted(enriched, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url'],
    ))


class HfmNuernbergDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfm_nuernberg_de',
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
    HfmNuernbergDeCrawler().run()


if __name__ == '__main__':
    main()
