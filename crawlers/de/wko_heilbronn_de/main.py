import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wko-heilbronn.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'konzerte')
SOURCE = 'Württembergisches Kammerorchester Heilbronn'
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
NON_GERMAN_CITIES = {'Amsterdam': 'NL', 'Fribourg': 'CH'}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u00ad', '')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
    )))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%d.%m.%y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])(?::([0-5]\d))?(?!\d)', clean_text(value))
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_location(value):
    parts = [part.strip() for part in clean_text(value).split(',') if part.strip()]
    if len(parts) < 2:
        return None
    city, venue_parts = parts[0], parts[1:]
    # The site names the Weilburg festival rather than the town in this listing.
    if city == 'Weilburger Schlosskonzerte':
        city = 'Weilburg'
    venue = ', '.join(venue_parts)
    return city, venue, NON_GERMAN_CITIES.get(city, 'DE')


def parse_card(card):
    title = clean_text(card.select_one('.event-name'))
    event_date = parse_date(card.select_one('.event-date .date'))
    link = card.select_one('.event-link1 a[href]')
    location_node = card.select_one('.event-description strong')
    location = parse_location(location_node)
    if not title or not event_date or not link or not location:
        return None
    city, venue, country_code = location
    teaser = clean_text(card.select_one('.event-description'))
    return {
        'title': title.replace('\n', ' - '),
        'date': event_date,
        'url': urljoin(SOURCE_URL, link['href']),
        'time_from': parse_time(card.select_one('.event-date .hrs')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': teaser or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_calendar(soup):
    return [record for card in soup.select('.event-teaser') if (record := parse_card(card))]


def detail_description(soup):
    main = soup.select_one('main .main-content-section') or soup.select_one('main')
    if not main:
        return None
    parts = []
    for frame in main.select('.frame-type-textmedia'):
        text = clean_text(frame)
        if not text:
            continue
        # Stop before the compact date/location/ticket block and site footer content.
        if re.search(r'\b\d{2}\.\d{2}\.\d{2}\b', text) or 'TICKET BESTELLEN' in text:
            break
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def enrich_records(session, records):
    descriptions = {}
    urls = sorted({record['url'] for record in records})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = detail_description(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape WKO concert detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    for record in records:
        detail = descriptions.get(record['url'])
        if detail:
            record['description'] = detail
    return records


class WkoHeilbronnDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wko_heilbronn_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        records = parse_calendar(get_soup(session, CALENDAR_URL))
        records = enrich_records(session, records)
        unique = {
            (item['url'], item['date'], item['time_from'], item['venue']): item
            for item in records
        }
        return sorted(unique.values(), key=lambda item: (
            item['date'], item['time_from'] or '', item['city'], item['title'], item['url'],
        ))


def main():
    WkoHeilbronnDeCrawler().run()


if __name__ == '__main__':
    main()
