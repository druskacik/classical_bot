import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.julianbliss.com/'
EVENT_SITEMAP_URL = f'{SOURCE_URL}event-pages-sitemap.xml'
SOURCE = 'Julian Bliss'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# Wix supplies one formatted address rather than structured address fields.
# These are the country names observed on this international artist's event pages,
# plus common aliases used by the same Google-backed location widget.
COUNTRY_CODES = {
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'china': 'CN',
    'czech republic': 'CZ',
    'czechia': 'CZ',
    'cyprus': 'CY',
    'denmark': 'DK',
    'estonia': 'EE',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'greece': 'GR',
    'hong kong': 'HK',
    'hungary': 'HU',
    'iceland': 'IS',
    'ireland': 'IE',
    'israel': 'IL',
    'italy': 'IT',
    'japan': 'JP',
    'latvia': 'LV',
    'liechtenstein': 'LI',
    'luxembourg': 'LU',
    'netherlands': 'NL',
    'new zealand': 'NZ',
    'norway': 'NO',
    'poland': 'PL',
    'portugal': 'PT',
    'romania': 'RO',
    'russia': 'RU',
    'singapore': 'SG',
    'slovakia': 'SK',
    'slovenia': 'SI',
    'south africa': 'ZA',
    'south korea': 'KR',
    'spain': 'ES',
    'sweden': 'SE',
    'switzerland': 'CH',
    'taiwan': 'TW',
    'the netherlands': 'NL',
    'uk': 'GB',
    'united kingdom': 'GB',
    'united states': 'US',
    'usa': 'US',
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from event_objects(item)
    elif isinstance(value, dict):
        if value.get('@type') == 'Event':
            yield value
        yield from event_objects(value.get('@graph', []))


def parse_country(address):
    final_part = address.rsplit(',', 1)[-1].strip().lower().rstrip('.')
    return COUNTRY_CODES.get(final_part)


def strip_postal_region(value, country_code):
    value = value.strip()
    patterns = {
        'AU': r'\s+(?:ACT|NSW|NT|QLD|SA|TAS|VIC|WA)\s+\d{4}$',
        'CA': r'\s+[A-Z]{2}\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d$',
        'GB': r'\s+[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$',
        'NZ': r'\s+\d{4}$',
    }
    if country_code in patterns:
        value = re.sub(patterns[country_code], '', value, flags=re.I)
    value = re.sub(r'^\d{4,6}\s+', '', value)
    return value.strip()


def parse_city(address, country_code):
    parts = [part.strip() for part in address.split(',') if part.strip()]
    if len(parts) < 2:
        return ''
    local = parts[:-1]

    # North American addresses normally put the state/province and postal code
    # in a separate final component after the city.
    if country_code in {'US', 'CA'} and len(local) >= 2:
        region = local[-1]
        if re.search(r'\b[A-Z]{2}\b', region, re.I) and re.search(r'\d', region):
            return strip_postal_region(local[-2], country_code)

    city = strip_postal_region(local[-1], country_code)
    # Some UK venue names contain a comma and are followed only by a postcode.
    if country_code == 'GB' and not city and len(local) >= 2:
        city = strip_postal_region(local[-2], country_code)
    return city


def parse_event_html(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    event = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        event = next(event_objects(data), None)
        if event:
            break
    if not event:
        return None

    title = clean_text(event.get('name'))
    description = clean_text(event.get('description')) or None
    start_value = clean_text(event.get('startDate'))
    try:
        start = datetime.fromisoformat(start_value.replace('Z', '+00:00'))
    except ValueError:
        return None

    location = event.get('location') or {}
    if not isinstance(location, dict):
        return None
    venue = clean_text(location.get('name'))
    address = clean_text(location.get('address'))
    country_code = parse_country(address)
    city = parse_city(address, country_code) if country_code else ''
    if not all((title, venue, city, country_code)):
        return None
    if venue.casefold() == city.casefold():
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def sitemap_urls(xml_text):
    soup = BeautifulSoup(xml_text, 'xml')
    urls = []
    for location in soup.select('url > loc'):
        url = clean_text(location)
        parsed = urlparse(url)
        if parsed.netloc == 'www.julianbliss.com' and parsed.path.startswith('/events/'):
            urls.append(url)
    return list(dict.fromkeys(urls))


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event_html(response.text, url)


class JulianblissComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='julianbliss_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(EVENT_SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        urls = sitemap_urls(response.text)
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Julian Bliss event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Julian Bliss event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required structured date, title, venue, city, or country is missing',
                    )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    JulianblissComCrawler().run()


if __name__ == '__main__':
    main()
