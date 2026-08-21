import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.johnlutheradams.net/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/list')
SOURCE = 'John Luther Adams'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The calendar is international and supplies country names rather than codes.
# These cover its established touring regions and common spelling variants.
COUNTRY_CODES = {
    'argentina': 'AR', 'australia': 'AU', 'austria': 'AT', 'belgium': 'BE',
    'brazil': 'BR', 'canada': 'CA', 'chile': 'CL', 'china': 'CN',
    'colombia': 'CO', 'croatia': 'HR', 'czech republic': 'CZ', 'czechia': 'CZ',
    'denmark': 'DK', 'estonia': 'EE', 'finland': 'FI', 'france': 'FR',
    'germany': 'DE', 'greece': 'GR', 'hungary': 'HU', 'iceland': 'IS',
    'india': 'IN', 'ireland': 'IE', 'israel': 'IL', 'italy': 'IT',
    'japan': 'JP', 'latvia': 'LV', 'lithuania': 'LT', 'luxembourg': 'LU',
    'mexico': 'MX', 'netherlands': 'NL', 'new zealand': 'NZ', 'norway': 'NO',
    'poland': 'PL', 'portugal': 'PT', 'romania': 'RO', 'serbia': 'RS',
    'singapore': 'SG', 'slovakia': 'SK', 'slovenia': 'SI',
    'south africa': 'ZA', 'south korea': 'KR', 'spain': 'ES', 'sweden': 'SE',
    'switzerland': 'CH', 'taiwan': 'TW', 'turkey': 'TR', 'uk': 'GB',
    'united kingdom': 'GB', 'usa': 'US', 'united states': 'US',
    'united states of america': 'US',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    lines = clean_text(value).splitlines()
    if not lines:
        return '', None
    try:
        event_date = datetime.strptime(lines[0], '%b %d %Y').date().isoformat()
    except ValueError:
        return '', None
    time_from = None
    if len(lines) > 1 and re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', lines[1]):
        time_from = datetime.strptime(lines[1], '%H:%M').strftime('%H:%M')
    return event_date, time_from


def event_description(container):
    heading = next(
        (node for node in container.select('.r-title') if clean_text(node).upper() == 'ABOUT THIS EVENT'),
        None,
    )
    if not heading:
        return None
    parts = []
    for sibling in heading.next_siblings:
        if getattr(sibling, 'get', None) and 'r-title' in (sibling.get('class') or []):
            break
        text = clean_text(sibling)
        if text:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('.red .two > .r-title')
    if not title_node:
        return None
    container = title_node.parent
    detail_blocks = container.select(':scope > div')
    if len(detail_blocks) < 3:
        return None

    event_date, time_from = parse_date_time(detail_blocks[0])
    location_lines = clean_text(detail_blocks[1]).splitlines()
    if len(location_lines) < 2 or ',' not in location_lines[-1]:
        return None
    venue = location_lines[0].strip()
    city, country_name = (part.strip() for part in location_lines[-1].rsplit(',', 1))
    country_code = COUNTRY_CODES.get(country_name.casefold())

    record = {
        'title': clean_text(title_node),
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': event_description(detail_blocks[2]),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    if not all(record[field] for field in (
        'title', 'date', 'url', 'venue', 'city', 'country_code', 'source_url', 'source'
    )):
        return None
    return record


class JohnLutherAdamsNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='johnlutheradams_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        urls = list(dict.fromkeys(
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select('a[href*="/calendar/event/"]')
            if link.get('href')
        ))

        records = []
        for url in urls:
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                record = parse_event(detail_response.content, url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete John Luther Adams calendar event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, city, or country is missing',
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch John Luther Adams calendar event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    JohnLutherAdamsNetCrawler().run()


if __name__ == '__main__':
    main()
