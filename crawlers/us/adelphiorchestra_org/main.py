import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.adelphiorchestra.org/'
SITEMAP_URL = f'{SOURCE_URL}event-pages-sitemap.xml'
SOURCE = 'Adelphi Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# Some Wix events use a municipality as location.name even though the street
# address identifies a specific venue. Keep these source-observed corrections
# narrow rather than treating a city as a venue.
VENUES_BY_ADDRESS = {
    '55 pyle st': 'River Dell High School',
    '132 kinnelon rd': 'Kinnelon Public Library',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_city(address):
    parts = [part.strip() for part in clean_text(address).split(',') if part.strip()]
    if parts and parts[-1].upper() in {'USA', 'US', 'UNITED STATES'}:
        parts.pop()
    if len(parts) < 3 or not re.fullmatch(r'[A-Z]{2}\s+\d{5}(?:-\d{4})?', parts[-1]):
        return ''
    return parts[-2]


def corrected_venue(address):
    normalized = address.casefold()
    return next(
        (venue for prefix, venue in VENUES_BY_ADDRESS.items() if normalized.startswith(prefix)),
        '',
    )


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = event_schema(soup)
    if not event:
        return None

    title = clean_text(event.get('name'))
    location = event.get('location') if isinstance(event.get('location'), dict) else {}
    address = clean_text(location.get('address'))
    venue = clean_text(location.get('name'))
    city = parse_city(address)
    venue = corrected_venue(address) or venue
    if venue.casefold() == city.casefold():
        venue = corrected_venue(address)

    try:
        start = datetime.fromisoformat(clean_text(event.get('startDate')).replace('Z', '+00:00'))
        event_date = start.date().isoformat()
        time_from = start.strftime('%H:%M')
    except (TypeError, ValueError):
        return None

    about = soup.select_one('[data-hook="about-section"]')
    description = clean_text(about) or clean_text(event.get('description')) or None
    if not title or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class AdelphiOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='adelphiorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.text, 'xml')
        urls = [
            clean_text(location)
            for location in sitemap.select('url > loc')
            if '/event-details-registration/' in clean_text(location)
        ]

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Adelphi Orchestra event detail',
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
                        'Skipped incomplete Adelphi Orchestra event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required schema, date, title, venue, or city is missing',
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    AdelphiOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
