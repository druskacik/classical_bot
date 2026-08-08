import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://calgaryphil.com/'
EVENTS_SITEMAP = f'{SOURCE_URL}events/sitemap/'
SOURCE = 'Calgary Philharmonic Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-CA,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def sitemap_urls(session):
    response = session.get(EVENTS_SITEMAP, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    return [clean_text(node.get_text()) for node in soup.select('loc')]


def event_payload(html):
    match = re.search(r'\bvar\s+EVENT\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
    if not match:
        return {}
    return json.loads(match.group(1))


def make_record(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    payload = event_payload(response.text)
    title = clean_text(payload.get('name'))
    venue_node = soup.select_one('.mpspx-event-single-location p')
    venue = clean_text(venue_node)
    url = response.url.rstrip('/')

    try:
        starts_at = datetime.fromisoformat(payload.get('start', ''))
    except (TypeError, ValueError):
        return None

    # The calendar is produced by Calgary's resident orchestra. Its sitemap
    # includes every performance it publishes, and venue names are Calgary
    # venues even though the detail template does not repeat the city.
    city = 'Calgary'
    if not title or not venue or not url:
        return None

    description_node = soup.select_one('.mpspx-event-single-custom1-inner')
    description = clean_text(description_node)
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'CA',
        'description': description or clean_text(payload.get('description')) or None,
    }


def fetch_record(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return make_record(response)


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = sitemap_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_record, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                log_message(
                    'Failed to scrape Calgary Phil concert',
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
                    'Skipped Calgary Phil concert with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )

    unique = {
        (record['url'], record['date'], record['time_from']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class CalgaryphilComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='calgaryphil_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CalgaryphilComCrawler().run()


if __name__ == '__main__':
    main()
