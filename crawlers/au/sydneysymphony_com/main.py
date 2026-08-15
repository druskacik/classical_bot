import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sydneysymphony.com/'
SOURCE = 'Sydney Symphony Orchestra'
API_URL = urljoin(SOURCE_URL, 'api/event-instances.json')
PAST_URL = urljoin(SOURCE_URL, 'concert-tickets/past-performances')
SYDNEY_TZ = ZoneInfo('Australia/Sydney')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-AU,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response


def event_description(soup):
    parts = []
    for element in soup.select('.rich-text'):
        text = clean_text(element)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def jsonld_graph(soup):
    graph = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        graph.extend(data.get('@graph', [data]) if isinstance(data, dict) else data)
    return [item for item in graph if isinstance(item, dict)]


def place_details(graph):
    places = {}
    for item in graph:
        if item.get('@type') != 'Place' or not item.get('@id'):
            continue
        address = item.get('address') or {}
        city = clean_text(address.get('addressLocality'))
        places[item['@id']] = (clean_text(item.get('name')), city)
    return places


def parse_utc_start(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
    except (AttributeError, TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_local_start(value):
    try:
        parsed = datetime.fromisoformat(value.rstrip('Z'))
    except (AttributeError, TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_detail(html):
    soup = BeautifulSoup(html, 'html.parser')
    graph = jsonld_graph(soup)
    places = place_details(graph)
    description = event_description(soup)
    records = []

    for item in graph:
        if item.get('@type') != 'Event' or not item.get('superEvent'):
            continue
        location = item.get('location') or {}
        venue, city = places.get(location.get('@id'), ('', ''))
        event_date, time_from = parse_utc_start(item.get('startDate'))
        title = clean_text(item.get('name'))
        url = urljoin(SOURCE_URL, item.get('url', ''))
        if not all((title, event_date, url, venue, city)):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'AU',
            'description': description,
        })
    return records


def parse_api_document(document, detail):
    event = document.get('event') or {}
    venue_data = event.get('venue') or {}
    title = clean_text(event.get('title'))
    venue = clean_text(venue_data.get('title'))
    event_date, time_from = parse_local_start(document.get('startDateLocalAsUTC'))
    city = detail.get('city') or infer_city(venue)
    url = urljoin(SOURCE_URL, document.get('url', ''))
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None if event.get('hideStartTime') else time_from,
        'venue': venue,
        'city': city,
        'country_code': 'AU',
        'description': detail.get('description') or clean_text(event.get('description')) or None,
    }


def infer_city(venue):
    # Every unnamed-locality venue currently exposed by the SSO calendar is in Sydney.
    # Touring venues are resolved from their detail-page Place address before this fallback.
    if venue:
        return 'Sydney'
    return ''


def detail_metadata(url):
    response = fetch(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    places = place_details(jsonld_graph(soup))
    city = next((city for _, city in places.values() if city), '')
    return {'description': event_description(soup), 'city': city}


def fetch_details(urls):
    results = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_metadata, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Sydney Symphony event detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                results[url] = {}
    return results


def archive_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    return sorted({
        urljoin(PAST_URL, link['href'])
        for link in soup.select('a[href^="/events/"]')
        if '/instances/' not in link['href']
    })


class SydneysymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sydneysymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AU',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url'],
    )

    def scrape(self):
        api_response = fetch(API_URL)
        payload = api_response.json()
        documents = payload.get('docs', [])

        event_urls = sorted({
            urljoin(SOURCE_URL, (document.get('event') or {}).get('url', ''))
            for document in documents
            if (document.get('event') or {}).get('url')
        })
        details = fetch_details(event_urls)
        records = []
        for document in documents:
            event_url = urljoin(SOURCE_URL, (document.get('event') or {}).get('url', ''))
            record = parse_api_document(document, details.get(event_url, {}))
            if record:
                records.append(record)

        past_response = fetch(PAST_URL)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch, url): url for url in archive_urls(past_response.text)
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(future.result().text))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Sydney Symphony archived event',
                        event='crawler_archive_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    SydneysymphonyComCrawler().run()


if __name__ == '__main__':
    main()
