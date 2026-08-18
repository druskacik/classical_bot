import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.highdesertchambermusic.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
EVENT_SITEMAP_URL = urljoin(SOURCE_URL, 'event-pages-sitemap.xml')
SOURCE = 'High Desert Chamber Music'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def event_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for link in soup.select('a[href*="/events-1/"]'):
        url = urljoin(CALENDAR_URL, link.get('href', '')).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == urlparse(SOURCE_URL).netloc and parsed.path.startswith('/events-1/'):
            links.add(url)
    return sorted(links)


def sitemap_event_links(xml):
    soup = BeautifulSoup(xml, 'xml')
    expected_host = urlparse(SOURCE_URL).netloc
    return sorted({
        clean_text(node.get_text()).split('#', 1)[0]
        for node in soup.find_all('loc')
        if urlparse(clean_text(node.get_text())).netloc == expected_host
        and urlparse(clean_text(node.get_text())).path.startswith('/events-1/')
    })


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def information_text(soup):
    heading = next(
        (node for node in soup.find_all(['h1', 'h2', 'h3']) if clean_text(node.get_text()) == 'Information'),
        None,
    )
    if not heading:
        return None

    parts = []
    for node in heading.find_all_next(['h1', 'h2', 'h3', 'p']):
        if node.name in {'h1', 'h2', 'h3'}:
            if node is not heading:
                break
            continue
        text = clean_text(node.get_text(' ', strip=True))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return None

    title = clean_text(BeautifulSoup(str(schema.get('name', '')), 'html.parser').get_text())
    start = clean_text(schema.get('startDate'))
    location = schema.get('location') or {}
    venue = clean_text(location.get('name'))
    address = clean_text(location.get('address'))
    city_match = re.search(r'(?:^|,)\s*([^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?(?:,|$)', address)
    city = clean_text(city_match.group(1)) if city_match else ''

    try:
        event_date = datetime.fromisoformat(start).date().isoformat()
    except ValueError:
        return None

    if not title or not venue or not city or venue.casefold() == city.casefold():
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': start[11:16] if re.match(r'\d{2}:\d{2}', start[11:16]) else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': information_text(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HighDesertChamberMusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='highdesertchambermusic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        sitemap = session.get(EVENT_SITEMAP_URL, timeout=45)
        sitemap.raise_for_status()

        links = sorted(set(event_links(response.text)) | set(sitemap_event_links(sitemap.text)))
        records = []
        for url in links:
            try:
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
                record = parse_event(detail.text, url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipping event with incomplete structured data',
                        event='crawler_event_skipped',
                        level='warning',
                        url=url,
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch event detail',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        if not records:
            log_message(
                'No parseable events found',
                event='crawler_empty_listing',
                level='warning',
                url=CALENDAR_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    HighDesertChamberMusicComCrawler().run()


if __name__ == '__main__':
    main()
