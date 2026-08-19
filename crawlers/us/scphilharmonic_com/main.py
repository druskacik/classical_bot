import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.scphilharmonic.com/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'South Carolina Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

EVENT_PATH_RE = re.compile(r'/(?:calendar/\d{2}-\d{2}-\d{4}|concerts/[^/]+)/[^/]+/$')
CITY_RE = re.compile(r'\b([A-Z][A-Za-z .\'’-]+?),\s*SC(?:\s+\d{5}(?:-\d{4})?)?\b')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_data(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            values = json.loads(script.string or '')
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if isinstance(value, dict) and value.get('@type') == 'Event':
                events.append(value)
    return events


def parse_start(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    return sorted({
        clean_text(node)
        for node in soup.select('loc')
        if EVENT_PATH_RE.search(clean_text(node))
    })


def parse_event(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Event page request failed',
            event='crawler_event_request_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    events = event_data(soup)
    if not events:
        return []

    title_node = soup.select_one('h1:not(.sr-only)')
    content_nodes = soup.select('.home-hero, section.two-column-content')
    content = '\n\n'.join(
        text for text in (clean_text(node) for node in content_nodes) if text
    )
    city_match = CITY_RE.search(content)
    city = clean_text(city_match.group(1)) if city_match else 'Columbia'

    records = []
    for data in events:
        title = clean_text(title_node) or clean_text(data.get('name'))
        event_date, time_from = parse_start(data.get('startDate'))
        location = data.get('location') or {}
        venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
        description = content or clean_text(data.get('description')) or None
        if not all((title, event_date, venue, city)):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
        })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_event, url): url for url in urls}
        for future in as_completed(futures):
            records.extend(future.result())

    if not records:
        log_message(
            'No event pages parsed from sitemap',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ScphilharmonicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='scphilharmonic_com',
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
            'country_code',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ScphilharmonicComCrawler().run()


if __name__ == '__main__':
    main()
