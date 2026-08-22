import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.stephanmoccio.com/'
TOUR_URL = f'{SOURCE_URL}tour/'
SOURCE = 'Stephan Moccio'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'brazil': 'BR',
    'canada': 'CA',
    'china': 'CN',
    'czech republic': 'CZ',
    'czechia': 'CZ',
    'denmark': 'DK',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'greece': 'GR',
    'hong kong': 'HK',
    'hungary': 'HU',
    'ireland': 'IE',
    'italy': 'IT',
    'japan': 'JP',
    'mexico': 'MX',
    'netherlands': 'NL',
    'new zealand': 'NZ',
    'norway': 'NO',
    'poland': 'PL',
    'portugal': 'PT',
    'singapore': 'SG',
    'south korea': 'KR',
    'spain': 'ES',
    'sweden': 'SE',
    'switzerland': 'CH',
    'united arab emirates': 'AE',
    'united kingdom': 'GB',
    'united states': 'US',
    'united states of america': 'US',
    'usa': 'US',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)).strip()


def country_code(country_name):
    name = clean_text(country_name).casefold().rstrip('.')
    return COUNTRY_CODES.get(name)


def event_links(soup):
    links = {}
    for row in soup.select('[data-gig-datetime]'):
        venue = clean_text(row.select_one('.tour-venue'))
        link = row.select_one('.tour-date a[href*="bandsintown.com/e/"]')
        raw_start = row.get('data-gig-datetime')
        if not venue or not link or not raw_start:
            continue
        try:
            date = datetime.fromisoformat(raw_start).date().isoformat()
        except ValueError:
            continue
        links[(date, venue.casefold())] = link.get('href')
    return links


def music_events(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'MusicEvent':
                yield item


def parse_event(item, links):
    location = item.get('location') or {}
    venue = clean_text(location.get('name'))
    address = location.get('address') or ''
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
        country = clean_text(address.get('addressCountry'))
    else:
        parts = [part.strip() for part in clean_text(address).split(',') if part.strip()]
        city = parts[0] if parts else ''
        country = parts[-1] if len(parts) > 1 else ''

    try:
        start = datetime.fromisoformat(clean_text(item.get('startDate')))
    except ValueError:
        return None

    code = country_code(country)
    url = links.get((start.date().isoformat(), venue.casefold()))
    if not venue or not city or not code or not url:
        return None

    return {
        'title': SOURCE,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M') if 'T' in clean_text(item.get('startDate')) else None,
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': clean_text(item.get('description')) or None,
    }


def get_concerts():
    log_message('Fetching tour page', event='crawler_url_fetch', url=TOUR_URL)
    response = requests.get(TOUR_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    links = event_links(soup)
    records = []
    for item in music_events(soup):
        record = parse_event(item, links)
        if record:
            records.append(record)
    return sorted(records, key=lambda record: (record['date'], record['time_from'] or '', record['venue']))


class StephanMoccioComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stephanmoccio_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        dedupe_subset=['date', 'time_from', 'venue', 'url'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    StephanMoccioComCrawler().run()


if __name__ == '__main__':
    main()
