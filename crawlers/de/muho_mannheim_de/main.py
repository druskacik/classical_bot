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


SOURCE_URL = 'https://www.muho-mannheim.de/'
SOURCE = 'Staatliche Hochschule für Musik und Darstellende Kunst Mannheim'
CALENDAR_URLS = (
    urljoin(SOURCE_URL, 'oeffentliche-veranstaltungen/kalender/'),
    urljoin(SOURCE_URL, 'oeffentliche-veranstaltungen/archiv/'),
)
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\xad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=16,
        pool_maxsize=16,
        max_retries=Retry(
            total=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(value):
    match = re.search(r'\b(\d{2}\.\d{2}\.\d{4})\b', clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_listing(soup):
    records = []
    for item in soup.select('.tx_events_results_item'):
        link = item.select_one('a[href*="/oeffentliche-veranstaltungen/kalender/"]')
        title_node = item.select_one('.tx_events_results_item_title, h3')
        event_date = parse_date(item.select_one('.tx_events_results_item_date'))
        city = clean_text(item.select_one('.tx_events_results_item_location'))
        if not link or not title_node or not event_date or not city:
            continue
        records.append({
            'title': clean_text(title_node),
            'date': event_date,
            'url': urljoin(SOURCE_URL, link.get('href', '')),
            'time_from': parse_time(item.select_one('.tx_events_results_item_time')),
            'city': city,
        })
    return records


def extract_venue(detail):
    for row in detail.select('.row.mt-5'):
        lines = [line.strip() for line in clean_text(row).splitlines() if line.strip()]
        postal_index = next((i for i, line in enumerate(lines) if re.search(r'\b\d{5}\b', line)), None)
        if postal_index is None or postal_index == 0:
            continue
        address_index = next(
            (i for i, line in enumerate(lines[:postal_index]) if re.search(r'\b\d+[a-zA-Z]?\b', line)),
            postal_index,
        )
        venue_lines = lines[:address_index]
        if venue_lines:
            return ' - '.join(venue_lines[:2])
    return None


def extract_description(detail):
    parts = []
    for node in detail.select('.row.mt-3 p'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def enrich_record(session, record):
    soup = get_soup(session, record['url'])
    detail = soup.select_one('.tx-events')
    if not detail:
        return None
    venue = extract_venue(detail)
    if not venue:
        return None
    detail_city = clean_text(detail.select_one('.tx_events_results_item_location'))
    if detail_city:
        record['city'] = detail_city
    record['venue'] = venue
    record['description'] = extract_description(detail)
    record['country_code'] = 'DE'
    record['source_url'] = SOURCE_URL
    record['source'] = SOURCE
    return record


class MuhoMannheimDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='muho_mannheim_de',
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
        session = make_session()
        listed = []
        for url in CALENDAR_URLS:
            try:
                listed.extend(parse_listing(get_soup(session, url)))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape MuHo Mannheim event listing',
                    event='crawler_page_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        unique = {(record['url'], record['date'], record['time_from']): record for record in listed}
        records = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {
                executor.submit(enrich_record, session, record): record
                for record in unique.values()
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    enriched = future.result()
                    if enriched:
                        records.append(enriched)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape MuHo Mannheim event detail',
                        event='crawler_item_failed', level='warning', url=record['url'],
                        error_type=type(error).__name__, error_message=str(error),
                    )

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['city'], item['title'], item['url'],
        ))


def main():
    MuhoMannheimDeCrawler().run()


if __name__ == '__main__':
    main()
