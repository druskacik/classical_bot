import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://southbendsymphony.org/'
SOURCE = 'South Bend Symphony Orchestra'
SITEMAP_URL = 'https://southbendsymphony.org/sitemap_index.xml'

HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def event_urls_from_sitemap(content):
    soup = BeautifulSoup(content, 'xml')
    urls = []
    for location in soup.find_all('loc'):
        url = clean_text(location.get_text())
        parsed = urlparse(url)
        if parsed.netloc == 'southbendsymphony.org' and parsed.path.startswith('/events/'):
            urls.append(url)
    return list(dict.fromkeys(urls))


def iter_jsonld_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from iter_jsonld_objects(item)
    elif isinstance(value, dict):
        yield value
        yield from iter_jsonld_objects(value.get('@graph', []))


def event_data_from_page(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in iter_jsonld_objects(value):
            event_type = item.get('@type')
            types = event_type if isinstance(event_type, list) else [event_type]
            if 'Event' in types:
                events.append(item)
    return events


def parse_start(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def city_from_location(location):
    if not isinstance(location, dict):
        return ''
    address = location.get('address')
    if isinstance(address, dict):
        return clean_text(address.get('addressLocality'))
    if not address:
        return ''
    lines = [clean_text(line) for line in re.split(r'[\r\n]+', str(address)) if clean_text(line)]
    for line in reversed(lines):
        match = re.fullmatch(r'([A-Za-z][A-Za-z .\'-]+),\s*IN\s+\d{5}(?:-\d{4})?', line)
        if match:
            return clean_text(match.group(1))
    # Known localities keep a flattened address from being mistaken for a city.
    flattened = clean_text(address)
    for city in ('South Bend', 'Mishawaka', 'Elkhart', 'Notre Dame', 'Plymouth', 'Niles'):
        if re.search(rf'\b{re.escape(city)},\s*(?:IN|MI)\s+\d{{5}}\b', flattened, re.IGNORECASE):
            return city
    return ''


def description_from_page(soup, event):
    description = soup.select_one('.event__description')
    if description:
        text = description.get_text('\n', strip=True)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        if text:
            return text
    return clean_text(event.get('description')) or None


def records_from_page(content, page_url):
    soup = BeautifulSoup(content, 'html.parser')
    records = []
    for event in event_data_from_page(soup):
        title = clean_text(event.get('name'))
        starts_at = parse_start(event.get('startDate'))
        location = event.get('location')
        venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
        city = city_from_location(location)
        event_url = clean_text(event.get('url')) or page_url
        if not title or not starts_at or not event_url or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': event_url,
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'description': description_from_page(soup, event),
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(SITEMAP_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    event_urls = event_urls_from_sitemap(response.content)
    if not event_urls:
        raise ValueError('South Bend Symphony sitemap contains no event pages')

    def fetch_page(url):
        try:
            page_response = session.get(url, headers=HEADERS, timeout=30)
            page_response.raise_for_status()
            return records_from_page(page_response.content, url)
        except requests.RequestException as error:
            log_message(
                'South Bend Symphony event page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return []

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_page, url) for url in event_urls]
        for future in as_completed(futures):
            records.extend(future.result())

    if not records:
        log_message(
            'No parseable South Bend Symphony events found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
    )


class SouthbendsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='southbendsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SouthbendsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
