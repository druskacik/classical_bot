import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hfm-weimar.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'besuchen/veranstaltungen/veranstaltungskalender/')
SOURCE = 'Hochschule für Musik FRANZ LISZT Weimar'
FIRST_ARCHIVE_YEAR = 2019
FUTURE_YEARS = 2
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
COUNTRY_MARKERS = {
    'Österreich': 'AT', 'Austria': 'AT',
    'Schweiz': 'CH', 'Switzerland': 'CH',
    'Tschechien': 'CZ', 'Czechia': 'CZ',
    'Frankreich': 'FR', 'France': 'FR',
    'Polen': 'PL', 'Poland': 'PL',
    'Italien': 'IT', 'Italy': 'IT',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
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
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def calendar_params(year, month):
    timestamp = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
    return {
        'ceid': 2325,
        'tx_jobase_pi5[ajaxSend]': 1,
        'tx_jobase_pi5[baseDate]': timestamp,
        'tx_jobase_pi5[loadDate]': timestamp,
        'type': 3324,
    }


def parse_date_time(value):
    text = clean_text(value).replace('\n', ' ')
    match = re.search(
        r'(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})'
        r'(?:\s+(\d{1,2}):(\d{2}))?',
        text,
    )
    if not match:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), int(match.group(2)), int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    event_time = f'{int(match.group(4)):02d}:{match.group(5)}' if match.group(4) else None
    return event_date, event_time


def parse_location(value):
    text = clean_text(value).replace('\n', ' ')
    text = re.sub(r'\s*,\s*', ', ', text).strip(' ,')
    if ',' not in text:
        return None, None, None
    city, venue = (part.strip() for part in text.split(',', 1))
    if (
        not city or not venue or city.casefold() == venue.casefold()
        or re.fullmatch(r'(?:Mo|Di|Mi|Do|Fr|Sa|So)\.', city)
        or re.search(r'\d{1,2}\.\d{1,2}\.\d{4}', venue)
    ):
        return None, None, None
    country_code = 'DE'
    for marker, code in COUNTRY_MARKERS.items():
        if re.search(rf'\b{re.escape(marker)}\b', text, re.I):
            country_code = code
            break
    city = re.sub(
        r'\s*\((?:' + '|'.join(map(re.escape, COUNTRY_MARKERS)) + r')\)\s*$',
        '', city, flags=re.I,
    ).strip()
    return city or None, venue or None, country_code


def parse_card(card):
    link = card.select_one('a.joVeranstaltungslink[href]')
    title = clean_text(card.select_one('.joVeranstaltungsTeaserHeadline'))
    address = card.select_one('.joVeranstaltungsTeaserAdresse')
    if not link or not title or not address:
        return None
    lines = [clean_text(line) for line in address.stripped_strings]
    event_date, event_time = parse_date_time(' '.join(lines))
    location = next(
        (
            line for line in reversed(lines)
            if ',' in line and not re.match(r'^(?:Mo|Di|Mi|Do|Fr|Sa|So)\.,', line)
        ),
        '',
    )
    city, venue, country_code = parse_location(location)
    if not event_date:
        return None
    return {
        'title': title.replace('\n', ' - '),
        'date': event_date,
        'url': urljoin(SOURCE_URL, link['href']),
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_month(session, year, month):
    response = session.get(CALENDAR_URL, params=calendar_params(year, month), timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return [record for card in soup.select('.eventitem') if (record := parse_card(card))]


def enrich_record(session, record):
    response = session.get(record['url'], timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    detail = soup.select_one('.eventlist.event-id')
    if not detail:
        return record
    description = clean_text(detail.select_one('.description'))
    record['description'] = description or None

    place = detail.select_one('.place')
    if place:
        city, venue, country_code = parse_location(place)
        if city and venue:
            record.update(city=city, venue=venue, country_code=country_code)
    return record


def get_concerts():
    session = make_session()
    months = [
        (year, month)
        for year in range(FIRST_ARCHIVE_YEAR, date.today().year + FUTURE_YEARS + 1)
        for month in range(1, 13)
    ]
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(fetch_month, session, year, month): (year, month)
            for year, month in months
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape HfM Weimar calendar month',
                    event='crawler_page_failed', level='warning',
                    url=CALENDAR_URL, year=year, month=month,
                    error_type=type(error).__name__, error_message=str(error),
                )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    enriched = []
    with ThreadPoolExecutor(max_workers=24) as executor:
        futures = {
            executor.submit(enrich_record, session, record): record
            for record in unique.values()
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched_record = future.result()
                if enriched_record['city'] and enriched_record['venue']:
                    enriched.append(enriched_record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape HfM Weimar event detail',
                    event='crawler_item_failed', level='warning', url=record['url'],
                    error_type=type(error).__name__, error_message=str(error),
                )
                if record['city'] and record['venue']:
                    enriched.append(record)
    return sorted(enriched, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url'],
    ))


class HfmWeimarDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfm_weimar_de',
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
    HfmWeimarDeCrawler().run()


if __name__ == '__main__':
    main()
