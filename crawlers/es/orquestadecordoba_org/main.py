import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html import unescape
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orquestadecordoba.org/'
CALENDAR_URL = f'{SOURCE_URL}programacion/calendario/'
SOURCE = 'Orquesta de Córdoba'
FIRST_ARCHIVE_YEAR = 2018

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def valid_url(value):
    parsed = urlparse(value or '')
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def calendar_month(session, year, month):
    response = session.get(
        CALENDAR_URL,
        params={'yr': year, 'month': month, 'time': 'month'},
        timeout=60,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    calendar = soup.select_one('.mc-main')
    if not calendar:
        return []

    schema_events = []
    for script in calendar.select('script[type="application/ld+json"]'):
        try:
            values = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(values, list):
            values = [values]
        schema_events.extend(
            event for event in values
            if isinstance(event, dict) and event.get('@type') == 'Event'
        )

    records = []
    for cell in calendar.select('td[id^="calendar-"]'):
        cell_date = cell.get('id', '').removeprefix('calendar-')
        if not cell_date.startswith(f'{year:04d}-{month:02d}-'):
            continue
        try:
            occurrence_date = date.fromisoformat(cell_date)
        except ValueError:
            continue
        for article in cell.select('article.calendar-event'):
            title = clean_text(article.select_one('.event-title'))
            candidates = []
            for event in schema_events:
                if clean_text(event.get('name')) != title:
                    continue
                try:
                    start_date = date.fromisoformat(clean_text(event.get('startDate'))[:10])
                    end_date = date.fromisoformat(clean_text(event.get('endDate'))[:10])
                except ValueError:
                    continue
                if start_date <= occurrence_date <= end_date:
                    candidates.append(event)
            event = candidates[0] if candidates else {}
            location = event.get('location') or {}
            address = location.get('address') or {} if isinstance(location, dict) else {}
            if not isinstance(address, dict):
                address = {}
            time_node = article.select_one('time.value-title')
            event_time = clean_text(time_node)
            link = article.select_one('a.calendar-link')
            records.append({
                'title': title,
                'date': cell_date,
                'url': clean_text(link.get('href')) if link else clean_text(event.get('url')),
                'time_from': event_time if re.fullmatch(r'\d{2}:\d{2}', event_time) else None,
                'venue': clean_text(location.get('name')) if isinstance(location, dict) else '',
                'city': clean_text(address.get('addressLocality')),
                'description': clean_text(event.get('description')) or None,
            })
    return records


def detail_text(session, url):
    if not url.startswith(f'{SOURCE_URL}eventos/'):
        return None
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('.descripcion-concierto')
    return clean_text(content) or None


def date_specific_location(description, event_date):
    """Read touring locations written as ``11/09: Venue. City``."""
    if not description:
        return None, None
    parsed = datetime.strptime(event_date, '%Y-%m-%d')
    prefix = rf'\b0?{parsed.day}/0?{parsed.month}\s*[:\-]\s*'
    match = re.search(prefix + r'([^\n]+)', description, flags=re.IGNORECASE)
    if not match:
        return None, None
    value = match.group(1).strip(' .')
    parts = [part.strip(' .') for part in re.split(r'\s*[|·]\s*|\.\s+', value) if part.strip(' .')]
    if len(parts) < 2:
        return None, None
    return ' - '.join(parts[:-1]), parts[-1]


def enrich_record(record, description):
    if description:
        record['description'] = description
        venue, city = date_specific_location(description, record['date'])
        if venue and city:
            record['venue'], record['city'] = venue, city
        if record['time_from'] is None:
            match = re.search(r'\b([01]?\d|2[0-3])[:.,]([0-5]\d)\s*h\b', description)
            if match:
                record['time_from'] = f'{int(match.group(1)):02d}:{match.group(2)}'

    if not valid_url(record['url']):
        parsed = datetime.strptime(record['date'], '%Y-%m-%d')
        record['url'] = f'{CALENDAR_URL}?yr={parsed.year}&month={parsed.month}&time=month'
    if not all(record.get(field) for field in ('title', 'date', 'url', 'venue', 'city')):
        return None
    record.update({
        'country_code': 'ES',
        'source_url': SOURCE_URL,
        'source': SOURCE,
    })
    return record


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    # The published archive begins in 2018. Two future years cover announced
    # seasons while keeping the number of empty calendar requests bounded.
    years = range(FIRST_ARCHIVE_YEAR, date.today().year + 3)
    months = [(year, month) for year in years for month in range(1, 13)]
    raw_records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(calendar_month, session, year, month): (year, month)
            for year, month in months
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                raw_records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{CALENDAR_URL}?yr={year}&month={month}&time=month',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['title'], record['date'], record['time_from'], record['url']): record
        for record in raw_records
    }
    detail_urls = {
        record['url'] for record in unique.values()
        if record['url'].startswith(f'{SOURCE_URL}eventos/')
    }
    descriptions = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_text, session, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = [
        enriched for record in unique.values()
        if (enriched := enrich_record(record, descriptions.get(record['url']))) is not None
    ]
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class OrquestadecordobaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestadecordoba_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
    OrquestadecordobaOrgCrawler().run()


if __name__ == '__main__':
    main()
