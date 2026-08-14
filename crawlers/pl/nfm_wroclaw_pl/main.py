import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nfm.wroclaw.pl/'
SOURCE = 'Narodowe Forum Muzyki im. Witolda Lutosławskiego'
REPERTOIRE_URL = urljoin(SOURCE_URL, 'repertuar')
ARCHIVE_URL = urljoin(SOURCE_URL, 'archiwum')
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)',
    'Accept-Language': 'pl-PL,pl;q=0.9',
}
EVENT_PATH = '/component/nfmcalendar/event/'


def clean_text(element):
    if element is None:
        return ''
    lines = []
    for line in element.get_text('\n', strip=True).replace('\xa0', ' ').splitlines():
        line = re.sub(r'\s+', ' ', line).strip()
        if line:
            lines.append(line)
    return '\n'.join(lines)


def canonical_event_url(value):
    url = urljoin(SOURCE_URL, value)
    return re.sub(r'/pl(?=/component/nfmcalendar/event/)', '', url)


def parse_location(value):
    value = re.sub(r'\s+', ' ', value).strip(' ,')
    if not value:
        return None, None, None

    parts = [part.strip() for part in value.split(',') if part.strip()]
    if parts and parts[0].casefold() == 'nfm':
        return value, 'Wrocław', 'PL'

    # Tour locations are rendered as "City, Venue". NFM-area locations that
    # omit a city are venue names and can safely use the institution's city.
    if len(parts) >= 2 and not re.search(
        r'\b(?:sala|kościół|katedra|bazylika|synagoga|teatr|centrum|muzeum|'
        r'filharmonia|zamek|pałac|park|studio|klub|akademia|opera)\b',
        parts[0], re.I,
    ):
        city = parts[0]
        venue = ', '.join(parts[1:])
    else:
        city = 'Wrocław'
        venue = value

    country_code = 'PL'
    foreign_cities = {
        'berlin': 'DE', 'dresden': 'DE', 'drezno': 'DE', 'görlitz': 'DE',
        'prague': 'CZ', 'praha': 'CZ', 'praga': 'CZ', 'vienna': 'AT',
        'wien': 'AT', 'london': 'GB', 'londyn': 'GB', 'paris': 'FR',
        'paryż': 'FR', 'brussels': 'BE', 'bruksela': 'BE',
    }
    country_code = foreign_cities.get(city.casefold(), country_code)
    return venue or None, city or None, country_code


def parse_time(element):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(element))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_archive_card(card):
    raw_url = card.get('data-link')
    title = clean_text(card.select_one('.media-box-title'))
    date_text = clean_text(card.select_one('.nfmMBEDate'))
    location = clean_text(card.select_one('.media-box-text'))
    try:
        event_date = datetime.strptime(date_text, '%d.%m.%Y').date().isoformat()
    except (TypeError, ValueError):
        return None
    venue, city, country_code = parse_location(location)
    if not all((raw_url, title, venue, city)):
        return None
    summary = clean_text(card.select_one('.media-box-description'))
    return {
        'title': title,
        'date': event_date,
        'url': canonical_event_url(raw_url),
        'time_from': parse_time(card.select_one('.nfmMBETime')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': summary or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_repertoire_cards(html, first_year):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    year = first_year
    previous_month = None
    for card in soup.select('.nfmELItem'):
        raw_url = card.get('data-link')
        if not raw_url:
            link = card.select_one(f'a[href*="{EVENT_PATH}"]')
            raw_url = link.get('href') if link else None
        title = clean_text(card.select_one('.nfmEDTitle'))
        date_text = clean_text(card.select_one('.nfmEDDate'))
        match = re.fullmatch(r'(\d{1,2})\.(\d{1,2})', date_text)
        if not match:
            continue
        day, month = map(int, match.groups())
        if previous_month is not None and month < previous_month:
            year += 1
        previous_month = month
        try:
            event_date = date(year, month, day).isoformat()
        except ValueError:
            continue
        location = clean_text(card.select_one('.nfmEDLoc'))
        venue, city, country_code = parse_location(location)
        if not all((raw_url, title, venue, city)):
            continue
        summary = clean_text(card.select_one('.nfmEDSubTitle'))
        records.append({
            'title': title,
            'date': event_date,
            'url': canonical_event_url(raw_url),
            'time_from': parse_time(card.select_one('.nfmEDTime')),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': summary or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.select_one('.nfmEvent .nfmContentSeccionBody')
    if body is None:
        return None
    # The right column is photography; the left column contains programme,
    # performers, and the editorial description needed by later extraction.
    content = body.select_one('.nfmCSLeftCol') or body
    return clean_text(content) or None


class NfmWroclawPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nfm_wroclaw_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def _get_soup(self, session, url):
        response = session.get(url, timeout=60)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')

    def _archive_records(self, session):
        archive = self._get_soup(session, ARCHIVE_URL)
        year_urls = sorted({
            urljoin(ARCHIVE_URL, item['data-link'])
            for item in archive.select('[data-link*="/archiwum/wydarzenia?y="]')
        })
        records = []
        for url in year_urls:
            try:
                page = self._get_soup(session, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch NFM archive year', event='crawler_page_failed',
                    level='warning', url=url, error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for card in page.select('.nfmMBEventArchive.event[data-link]'):
                record = parse_archive_card(card)
                if record:
                    records.append(record)
        return records

    def _repertoire_records(self, session):
        records = []
        today = date.today()
        for event_type in ('nfm', 'other'):
            response = session.get(
                REPERTOIRE_URL, params={'type': event_type}, timeout=60,
            )
            response.raise_for_status()
            parsed = parse_repertoire_cards(response.text, today.year)
            # The feed begins at or after today. A stale item from December
            # around New Year belongs to the previous year, not the next one.
            for record in parsed:
                if record['date'] < today.isoformat():
                    try:
                        adjusted = date.fromisoformat(record['date']).replace(
                            year=today.year - 1
                        )
                    except ValueError:
                        continue
                    record['date'] = adjusted.isoformat()
            records.extend(parsed)
        return records

    def _add_current_descriptions(self, session, records):
        today = date.today().isoformat()
        current = [record for record in records if record['date'] >= today]

        def fetch(record):
            response = session.get(record['url'], timeout=60)
            response.raise_for_status()
            return parse_detail_description(response.text)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch, record): record for record in current}
            for future in as_completed(futures):
                record = futures[future]
                try:
                    description = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch NFM event detail',
                        event='crawler_page_failed', level='warning',
                        url=record['url'], error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if description:
                    record['description'] = description

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = self._archive_records(session)
        records.extend(self._repertoire_records(session))

        by_key = {}
        for record in records:
            key = (record['url'], record['date'], record['time_from'])
            by_key[key] = record
        records = list(by_key.values())
        self._add_current_descriptions(session, records)
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    NfmWroclawPlCrawler().run()


if __name__ == '__main__':
    main()
