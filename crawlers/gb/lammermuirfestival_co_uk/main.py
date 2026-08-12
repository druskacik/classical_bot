import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lammermuirfestival.co.uk/'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
SOURCE = 'Lammermuir Festival'

HEADERS = {
    # The site protects ordinary automated browsers with Cloudflare, while its
    # public sitemap and event pages are intentionally available to crawlers.
    'User-Agent': 'Googlebot',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    text = html.unescape(str(value))
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    urls = []
    for node in soup.select('url > loc'):
        url = clean_text(node.get_text())
        if re.match(r'^https://www\.lammermuirfestival\.co\.uk/event/[^/]+/$', url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def json_ld_items(value):
    if isinstance(value, list):
        for item in value:
            yield from json_ld_items(item)
    elif isinstance(value, dict):
        yield value
        for key in ('@graph',):
            if key in value:
                yield from json_ld_items(value[key])


def music_event(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in json_ld_items(data):
            event_type = item.get('@type')
            types = event_type if isinstance(event_type, list) else [event_type]
            if 'MusicEvent' in types:
                return item
    return None


def parse_datetime(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def venue_and_city(location):
    if not isinstance(location, dict):
        return None, None

    name = clean_text(location.get('name'))
    address = location.get('address') or {}
    city = clean_text(address.get('addressLocality')) if isinstance(address, dict) else ''
    if not name:
        return None, None

    parts = [part.strip() for part in name.split(',') if part.strip()]
    venue = parts[0]
    if not city and len(parts) > 1:
        locality = parts[-1]
        locality = re.sub(r'\s+[–-]\s+.*$', '', locality)
        locality = re.sub(r'^(?:near|nr\.?|near to)\s+', '', locality, flags=re.IGNORECASE)
        # Some venue labels include both a village and "nr <town>". The
        # village is the more precise locality when present.
        if len(parts) > 2 and re.match(r'^(?:near|nr\.?)\s+', parts[-1], re.IGNORECASE):
            locality = parts[-2]
        city = clean_text(locality)

    if not venue or not city or venue.casefold() == city.casefold():
        return None, None
    return venue, city


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    event = music_event(soup)
    if not event:
        return None

    attendance = event.get('eventAttendanceMode')
    if attendance and 'OfflineEventAttendanceMode' not in str(attendance):
        return None

    title = clean_text(event.get('name'))
    parsed_datetime = parse_datetime(event.get('startDate'))
    venue, city = venue_and_city(event.get('location'))
    if not title or not parsed_datetime or not venue or not city:
        return None

    date, time_from = parsed_datetime
    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': description,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result().content, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Lammermuir Festival event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class LammermuirFestivalCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lammermuirfestival_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
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
        return get_concerts()


def main():
    LammermuirFestivalCrawler().run()


if __name__ == '__main__':
    main()
