import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.vlaamsradiokoor.be/'
AGENDA_URL = urljoin(SOURCE_URL, 'concerten')
SOURCE = 'Vlaams Radiokoor'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-BE,nl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def country_and_city(value):
    text = clean_text(value)
    match = re.fullmatch(r'(.+?)\s*\(([A-Z]{2})\)', text)
    if match:
        return match.group(2), match.group(1).strip()
    return 'BE', text


def listing_records(soup):
    records = []
    seen_urls = set()
    for article in soup.select('article'):
        link = article.select_one('h3 a[href*="/concerten/"]')
        time = article.select_one('time[datetime]')
        location = time.find_parent('div', class_='mb-4') if time else None
        if not link or not time or not location:
            continue

        location_lines = location.find_all('div', recursive=False)
        if len(location_lines) < 3:
            continue
        country_code, city = country_and_city(location_lines[0])
        venue = clean_text(location_lines[1])
        title = clean_text(link)
        url = urljoin(SOURCE_URL, link.get('href'))
        time_match = re.search(r'\b(\d{2}:\d{2})\b', clean_text(time))
        try:
            event_date = date.fromisoformat(time.get('datetime', '')[:10]).isoformat()
        except ValueError:
            continue
        if not title or not city or not venue or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_match.group(1) if time_match else None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(soup):
    main = soup.select_one('main')
    if not main:
        return None

    parts = []
    # Editorial copy is published in text-editor blocks. Include the programme
    # block as well: it contains the composer/work data needed downstream.
    for block in main.select('.text-editor'):
        text = clean_text(block)
        lowered = text.lower()
        if not text or lowered.startswith(('praktisch', 'locatie', 'tickets')):
            continue
        if text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    agenda = get_soup(session, AGENDA_URL)
    records_by_url = {record['url']: record for record in listing_records(agenda)}

    # The unfiltered calendar lists upcoming events, while its date picker also
    # publishes archive dates. Each of those dates can be requested through the
    # normal HTML filter, including dates with more than one performance.
    date_picker = agenda.select_one('[data-event-dates]')
    try:
        published_dates = set(json.loads(date_picker.get('data-event-dates', '[]')))
    except (AttributeError, json.JSONDecodeError, TypeError):
        published_dates = set()
    listed_dates = {record['date'] for record in records_by_url.values()}

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_soup, session, AGENDA_URL, {'eventDate': event_date}): event_date
            for event_date in published_dates - listed_dates
        }
        for future in as_completed(futures):
            event_date = futures[future]
            try:
                for record in listing_records(future.result()):
                    records_by_url[record['url']] = record
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape archived calendar date',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{AGENDA_URL}?eventDate={event_date}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = list(records_by_url.values())

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, record['url']): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = detail_description(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
    )


class VlaamsRadiokoorBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='vlaamsradiokoor_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    VlaamsRadiokoorBeCrawler().run()


if __name__ == '__main__':
    main()
