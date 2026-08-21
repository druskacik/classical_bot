import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://danielhope.com/'
CALENDAR_URL = f'{SOURCE_URL}performances/'
SOURCE = 'Daniel Hope'

COUNTRY_CODES = {
    'argentina': 'AR', 'australia': 'AU', 'austria': 'AT', 'belgium': 'BE',
    'brazil': 'BR', 'canada': 'CA', 'china': 'CN', 'croatia': 'HR',
    'czech republic': 'CZ', 'czechia': 'CZ', 'denmark': 'DK', 'estonia': 'EE',
    'finland': 'FI', 'france': 'FR', 'germany': 'DE', 'greece': 'GR',
    'hungary': 'HU', 'ireland': 'IE', 'israel': 'IL', 'italy': 'IT',
    'japan': 'JP', 'latvia': 'LV', 'liechtenstein': 'LI', 'lithuania': 'LT',
    'luxembourg': 'LU', 'mexico': 'MX', 'monaco': 'MC', 'netherlands': 'NL',
    'new zealand': 'NZ', 'norway': 'NO', 'poland': 'PL', 'portugal': 'PT',
    'romania': 'RO', 'serbia': 'RS', 'singapore': 'SG', 'slovakia': 'SK',
    'slovenia': 'SI', 'south africa': 'ZA', 'south korea': 'KR', 'spain': 'ES',
    'sweden': 'SE', 'switzerland': 'CH', 'taiwan': 'TW', 'turkey': 'TR',
    'united arab emirates': 'AE', 'united kingdom': 'GB', 'uk': 'GB',
    'united states': 'US', 'united states of america': 'US', 'usa': 'US',
}

TIME_RE = re.compile(r'\((\d{1,2}):(\d{2})\)\s*$')


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_location(node):
    if node is None:
        return None
    location_parts = []
    for child in node.children:
        if getattr(child, 'name', None) == 'span':
            break
        if isinstance(child, NavigableString):
            location_parts.append(str(child))
    location = ''.join(location_parts)
    parts = [part.strip() for part in clean_text(location).split(',') if part.strip()]
    if len(parts) < 2:
        return None
    country_code = COUNTRY_CODES.get(parts[-1].lower())
    city = parts[0]
    if not country_code or not city:
        return None
    return city, country_code


def parse_venue_and_time(value):
    venue = clean_text(value)
    match = TIME_RE.search(venue)
    if not match:
        return venue, None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return venue, None
    return venue[:match.start()].strip(), f'{hour:02d}:{minute:02d}'


def parse_performances(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('ul.events-page-list li.event-item'):
        date_text = clean_text(item.select_one('.date-title'))
        venue, time_from = parse_venue_and_time(item.select_one('.perform-title'))
        location = parse_location(item.select_one('.city'))
        if location is None and venue.lower().startswith('elbphilharmonie hamburg'):
            location = ('Hamburg', 'DE')
        notes = clean_text(item.select_one('.notes'))
        link = item.select_one('a.ticket-link[href]')

        try:
            event_date = datetime.strptime(date_text, '%B %d, %Y').date().isoformat()
        except ValueError:
            continue
        if not (venue and location):
            continue

        city, country_code = location
        url = clean_text(link.get('href')) if link else CALENDAR_URL
        title = notes or f'Daniel Hope at {venue}'
        presenter_node = item.select_one('.city span')
        presenter = ''
        if presenter_node:
            presenter = clean_text(''.join(str(node) for node in presenter_node.next_siblings))
        description_parts = [part for part in (notes, presenter) if part]
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': '\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class DanielHopeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='danielhope_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        dedupe_subset=['date', 'time_from', 'venue', 'city', 'url'],
    )

    def scrape(self):
        log_message('Fetching Daniel Hope performances', event='crawler_url_fetch', url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        records = parse_performances(response.content)
        log_message(
            'Parsed Daniel Hope performances',
            event='crawler_scrape_completed',
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return records


def main():
    DanielHopeCrawler().run()


if __name__ == '__main__':
    main()
