import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gbs.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts'
SOURCE = 'Greater Bridgeport Symphony'
PLUGIN_ROOT = 'https://plugin.vbotickets.com'
SITE_ID = '6F4CB56D-104B-4C65-A2C4-716B08EFF3CC'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_datetime(value):
    text = clean_text(value)
    match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})\s*@\s*(\d{1,2}:\d{2}\s*[AP]M)', text, re.I)
    if not match:
        return None, None
    try:
        parsed = datetime.strptime(' '.join(match.groups()), '%m/%d/%Y %I:%M %p')
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def city_from_venue(address, venue):
    text = clean_text(address)
    match = re.search(r'(?:^|,)\s*([^,]+),\s*(?:Connecticut|CT)\b', text, re.I)
    if match:
        city = re.sub(r'^in\s+', '', match.group(1), flags=re.I).strip()
        if city:
            return city
    if re.search(r'\bBridgeport\b', f'{text} {venue}', re.I):
        return 'Bridgeport'
    return None


def parse_feed(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('.EventListWrapper[data-event-name]'):
        title = clean_text(item.get('data-event-name'))
        date, time_from = parse_datetime(item.select_one('.TextEventDate'))
        venue = clean_text(item.select_one('.TextVenueName'))
        address = clean_text(item.select_one('.TextVenueAddress'))
        city = city_from_venue(address, venue)
        link = item.select_one('.HeaderEventName a[href]')
        description = clean_text(item.select_one('.EventIntroText')) or None
        if not title or not date or not venue or not city:
            continue
        # Sold-out archive entries no longer have a ticket-detail link.
        url = requests.compat.urljoin(PLUGIN_ROOT, link.get('href')) if link else CONCERTS_URL
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    loader_url = f'{PLUGIN_ROOT}/plugin/loadplugin'
    response = session.get(loader_url, params={
        'siteid': SITE_ID,
        'page': 'ListEvents',
        'parent': 'www.gbs.org',
        'parenturl': CONCERTS_URL,
    }, timeout=45)
    response.raise_for_status()
    match = re.search(r'/plugin/events\?s=([a-z0-9-]+)', response.text, re.I)
    if not match:
        raise ValueError('VBO Tickets session identifier was not found')

    session_id = match.group(1)
    session.get(f'{PLUGIN_ROOT}/plugin/events', params={'s': session_id}, timeout=45).raise_for_status()
    records = []
    for event_type in ('current', 'past'):
        response = session.get(
            f'{PLUGIN_ROOT}/Plugin/events/showevents',
            params={
                'ViewType': 'list',
                'EventType': event_type,
                'day': '',
                's': session_id,
            },
            timeout=45,
        )
        response.raise_for_status()
        records.extend(parse_feed(response.text))

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['url']))
    if not result:
        log_message(
            'No event records found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return result


class GbsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gbs_org',
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
        return scrape_concerts()


def main():
    GbsOrgCrawler().run()


if __name__ == '__main__':
    main()
