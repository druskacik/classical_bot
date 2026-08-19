import json
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.overtoneindustries.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'buy-tickets')
SOURCE = 'Overtone Industries'
ZEFFY_HOST = 'www.zeffy.com'
OCCURRENCES_API = 'https://api.zeffy.com/_new/trpc/form_getActiveTicketingOccurrences'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_RE = re.compile(
    r'\bat\s+([^().,]{2,100}?)(?:\s*\([^)]*\)|,\s*(?:presented|hosted|located)\b|\.)',
    re.IGNORECASE,
)
ADDRESS_CITY_RE = re.compile(r',\s*([^,]+?),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b')


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def zeffy_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for link in soup.select('a[href]'):
        href = urljoin(EVENTS_URL, link.get('href', ''))
        if ZEFFY_HOST in href and '/ticketing/' in href and href not in links:
            links.append(href)
    return links


def ticketing_data(html):
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one('#__NEXT_DATA__')
    if not node or not node.string:
        return None
    try:
        return json.loads(node.string)['props']['pageProps']['ticketing']
    except (KeyError, TypeError, json.JSONDecodeError):
        return None


def occurrences(session, ticketing_id):
    response = session.get(
        OCCURRENCES_API,
        params={'input': json.dumps({'ticketingId': ticketing_id}, separators=(',', ':'))},
        timeout=45,
    )
    response.raise_for_status()
    return response.json().get('result', {}).get('data', [])


def event_description(data):
    fields = data.get('ticketingFields') or []
    if not fields:
        return None
    html = fields[0].get('sanitizedDescription') or fields[0].get('description') or ''
    text = clean_text(BeautifulSoup(html, 'html.parser').get_text('\n', strip=True))
    return text or None


def event_title(data):
    fields = data.get('ticketingFields') or []
    return clean_text(fields[0].get('title')) if fields else ''


def event_venue(description):
    match = VENUE_RE.search(description or '')
    return clean_text(match.group(1)) if match else ''


def event_city(data):
    organization = data.get('organization') or {}
    city = clean_text(organization.get('city'))
    if city:
        return city
    match = ADDRESS_CITY_RE.search(clean_text(data.get('address')))
    return clean_text(match.group(1)) if match else ''


def parse_occurrence(data, item, url):
    occurrence = item.get('occurrence') or {}
    start_value = occurrence.get('startUtc')
    timezone_name = data.get('eventTimezone') or 'America/Los_Angeles'
    try:
        start = datetime.fromisoformat(start_value.replace('Z', '+00:00')).astimezone(
            ZoneInfo(timezone_name)
        )
    except (AttributeError, TypeError, ValueError, KeyError):
        return None

    description = event_description(data)
    record = {
        'title': event_title(data),
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': event_venue(description),
        'city': event_city(data),
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    if not all(record[key] for key in ('title', 'date', 'url', 'venue', 'city')):
        return None
    return record


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()

    records = []
    skipped = 0
    for url in zeffy_links(response.text):
        detail_response = session.get(url, timeout=45)
        detail_response.raise_for_status()
        data = ticketing_data(detail_response.text)
        if not data:
            skipped += 1
            continue
        for item in occurrences(session, data.get('id')):
            record = parse_occurrence(data, item, url)
            if record:
                records.append(record)
            else:
                skipped += 1

    if skipped:
        log_message(
            'Skipped ticket occurrences missing required fields',
            event='crawler_records_skipped',
            level='warning',
            url=EVENTS_URL,
            record_count=skipped,
        )
    if not records:
        log_message(
            'No valid active ticket occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class OvertoneIndustriesOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='overtoneindustries_org',
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
        return scrape_events()


def main():
    OvertoneIndustriesOrgCrawler().run()


if __name__ == '__main__':
    main()
