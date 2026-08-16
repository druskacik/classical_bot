import json
import html
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.georgiaphilharmonic.org/'
SITEMAP_URL = f'{SOURCE_URL}event-pages-sitemap.xml'
SOURCE = 'Georgia Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def event_urls(sitemap):
    soup = BeautifulSoup(sitemap, 'xml')
    urls = []
    for location in soup.find_all('loc'):
        url = clean_text(location)
        parsed = urlparse(url)
        if (
            parsed.netloc in {'www.georgiaphilharmonic.org', 'georgiaphilharmonic.org'}
            and parsed.path.startswith('/event-details/')
        ):
            urls.append(url)
    return list(dict.fromkeys(urls))


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


def parse_start(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def city_from_address(address):
    # Wix emits US addresses as "street, city, ST ZIP, USA".
    parts = [part.strip() for part in clean_text(address).split(',') if part.strip()]
    for index, part in enumerate(parts):
        if index and re.fullmatch(r'[A-Z]{2}\s+\d{5}(?:-\d{4})?', part):
            return parts[index - 1]
    return ''


def description_from_page(soup, schema_description=''):
    section = soup.select_one('[data-hook="about-section"]')
    if not section:
        return clean_text(schema_description) or None
    heading = section.select_one('[data-hook="about"]')
    if heading:
        heading.extract()
    parts = [clean_text(schema_description), clean_text(section)]
    description = '\n\n'.join(dict.fromkeys(part for part in parts if part))
    return description or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return None
    location = schema.get('location') or {}
    title = clean_text(schema.get('name'))
    event_date, time_from = parse_start(schema.get('startDate'))
    venue = clean_text(location.get('name'))
    city = city_from_address(location.get('address'))
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description_from_page(soup, schema.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class GeorgiaPhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='georgiaphilharmonic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        sitemap_response = session.get(SITEMAP_URL, timeout=45)
        sitemap_response.raise_for_status()
        urls = event_urls(sitemap_response.text)
        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_event(response.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Georgia Philharmonic event',
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
                    'Skipped incomplete Georgia Philharmonic event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required structured title, date, venue, or city is missing',
                )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    GeorgiaPhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
