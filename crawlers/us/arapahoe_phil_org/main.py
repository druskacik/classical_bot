import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.symphonyrockies.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Symphony of the Rockies'

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
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))


def event_json(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('startDate') and candidate.get('location'):
                return candidate
    return None


def parse_start(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_location(location):
    if not isinstance(location, dict):
        return None
    venue = clean_text(location.get('name'))
    address = clean_text(location.get('address'))
    lines = [line for line in address.splitlines() if line]
    city = ''
    if len(lines) >= 2:
        # Squarespace addresses end in a country line; the preceding line is
        # consistently "City, ST, postal code" (or the state name spelled out).
        locality = lines[-2] if lines[-1].lower() in ('united states', 'usa') else lines[-1]
        city = locality.split(',', 1)[0].strip()
    if not venue or not city:
        return None
    return venue, city


def detail_description(soup, event):
    content = soup.select_one('.eventitem-column-content')
    description = clean_text(content)
    if description:
        return description
    return clean_text(event.get('description')) or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = event_json(soup)
    if not event:
        return None

    title = clean_text(soup.select_one('.eventitem-title'))
    if not title:
        title = re.sub(r'\s+[—-]\s+Symphony of the Rockies$', '', clean_text(event.get('name')))
    event_date, time_from = parse_start(event.get('startDate'))
    location = parse_location(event.get('location'))
    if not title or not event_date or not location:
        return None
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': canonical_url(url),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': detail_description(soup, event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def listing_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.select('article.eventlist-event a.eventlist-title-link[href]'):
        url = canonical_url(urljoin(EVENTS_URL, link['href']))
        if url.startswith(f'{EVENTS_URL}/'):
            urls.append(url)
    return list(dict.fromkeys(urls))


class ArapahoePhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='arapahoe_phil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(EVENTS_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Symphony of the Rockies event listing',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = listing_urls(response.text)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail = future.result()
                    detail.raise_for_status()
                    record = parse_detail(detail.text, url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Symphony of the Rockies event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ArapahoePhilOrgCrawler().run()


if __name__ == '__main__':
    main()
