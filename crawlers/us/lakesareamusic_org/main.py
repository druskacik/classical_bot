import html
import json
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lakesareamusic.org/'
SOURCE = 'Lakes Area Music Festival'
CALENDAR_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
TIMEZONE = ZoneInfo('America/Chicago')

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
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def schema_location_fallback(soup):
    """Read location fields even when the site's later JSON-LD image is invalid."""
    for node in soup.select('script[type="application/ld+json"]'):
        text = node.string or node.get_text()
        if '"@type": "Event"' not in text or '"location"' not in text:
            continue
        location_match = re.search(r'"location"\s*:\s*\{(.*?)\}\s*,\s*"image"', text, re.S)
        if not location_match:
            continue
        block = location_match.group(1)
        name = re.search(r'"name"\s*:\s*"([^"]*)"', block)
        city = re.search(r'"addressLocality"\s*:\s*"([^"]*)"', block)
        return clean_text(name.group(1) if name else ''), clean_text(city.group(1) if city else '')
    return '', ''


def detail_data(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    schema = event_schema(soup)
    location = schema.get('location') if isinstance(schema.get('location'), dict) else {}
    address = location.get('address') if isinstance(location.get('address'), dict) else {}

    # Some pages contain malformed JSON-LD because the image value embeds HTML.
    # The first-party event widget exposes the same location in stable fields.
    venue_node = soup.select_one('#event-where .occurrence-venue-name')
    city_node = soup.select_one('#event-where .venue-city')
    fallback_venue, fallback_city = schema_location_fallback(soup)
    venue = clean_text(location.get('name')) or fallback_venue or clean_text(
        venue_node.get_text(' ', strip=True) if venue_node else ''
    )
    city = clean_text(address.get('addressLocality')) or fallback_city
    if not city and city_node:
        city = clean_text(city_node.get_text(' ', strip=True)).split(',', 1)[0].strip()

    canonical = soup.select_one('link[rel="canonical"]')
    article = soup.select_one('main article')
    return {
        'url': canonical.get('href') if canonical and canonical.get('href') else response.url,
        'venue': venue,
        'city': city,
        # The article includes the programme, long description, and artist context.
        'description': clean_text(article.get_text('\n', strip=True)) if article else None,
    }


def calendar_events(session):
    response = session.post(
        CALENDAR_URL,
        data={
            'action': 'vem_get_events',
            'id': '883',
            'event': '0',
            # Include the site's retained archive as well as long-range future events.
            'start': '946684800',  # 2000-01-01 UTC
            'end': '4133980799',  # 2100-12-31 UTC
            'moment': str(int(datetime.now().timestamp())),
            'futureOnly': 'false',
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get('events', [])


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = calendar_events(session)
    details = {}
    records = []

    for event in events:
        raw_url = html.unescape(event.get('url') or '')
        if not raw_url or not event.get('start'):
            continue
        try:
            start = datetime.fromtimestamp(int(event['start']), TIMEZONE)
        except (ValueError, TypeError, OSError):
            continue

        event_id = str(event.get('eventId') or raw_url)
        if event_id not in details:
            try:
                details[event_id] = detail_data(session, raw_url)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=raw_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                details[event_id] = {}
        detail = details[event_id]
        title = clean_text(event.get('title'))
        venue = detail.get('venue', '')
        city = detail.get('city', '')
        url = detail.get('url', raw_url)
        if not title or not venue or not city or not url:
            continue

        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': detail.get('description') or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No valid calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class LakesAreaMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lakesareamusic_org',
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
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    LakesAreaMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
