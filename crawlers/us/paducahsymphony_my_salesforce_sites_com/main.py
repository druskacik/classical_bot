import json
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://paducahsymphony.my.salesforce-sites.com/ticket/'
SOURCE = 'Paducah Symphony Orchestra'
CITY = 'Paducah'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUES = {
    'a0TU0000000EZwQMAW': 'The Carson Center for the Performing Arts',
    'a0TRl00000da6qRMAQ': 'Williams Family Symphony Hall',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for node in soup.select('iframe, script, style'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def remoting_config(page):
    marker = 'RemotingProviderImpl('
    start = page.find(marker)
    if start < 0:
        raise ValueError('Visualforce remoting configuration was not found')
    config, _ = json.JSONDecoder().raw_decode(page[start + len(marker):])
    methods = config['actions']['PatronTicket.Controller_PublicTicketApp']['ms']
    method = next(item for item in methods if item['name'] == 'fetchEvents')
    return config, method


def fetch_events(session):
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    config, method = remoting_config(response.text)

    payload = {
        'action': 'PatronTicket.Controller_PublicTicketApp',
        'method': 'fetchEvents',
        'data': [SOURCE_URL, '', '', 'Expanded'],
        'type': 'rpc',
        'tid': 1,
        'ctx': {
            'csrf': method['csrf'],
            'vid': config['vf']['vid'],
            'ns': method['ns'],
            'ver': method['ver'],
            'authorization': method['authorization'],
        },
    }
    response = session.post(
        f'{SOURCE_URL}apexremote',
        json=payload,
        headers={
            'X-User-Agent': 'Visualforce-Remoting',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': SOURCE_URL,
        },
        timeout=45,
    )
    response.raise_for_status()
    envelope = response.json()[0]
    if envelope.get('statusCode') != 200 or not isinstance(envelope.get('result'), list):
        raise ValueError('Unexpected fetchEvents response')
    return envelope['result']


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)
    records = []

    for event in events:
        categories = {item.strip() for item in (event.get('category') or '').split(';')}
        if event.get('type') != 'Tickets' or 'Classical' not in categories:
            continue

        description_parts = [clean_html(event.get('description')), clean_html(event.get('detail'))]
        description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None
        for instance in event.get('instances') or []:
            formatted = instance.get('formattedDates') or {}
            compact_date = str(formatted.get('YYYYMMDD') or '')
            venue = VENUES.get(instance.get('venueId'))
            title = (instance.get('eventName') or event.get('name') or '').strip()
            url = instance.get('purchaseUrl') or event.get('purchaseUrl')
            if not re.fullmatch(r'\d{8}', compact_date) or not title or not url or not venue:
                continue

            time_match = re.fullmatch(r'(\d{1,2}):(\d{2})\s*([AP]M)', formatted.get('TIME_STRING') or '')
            time_from = None
            if time_match:
                hour, minute, meridiem = time_match.groups()
                hour = int(hour) % 12 + (12 if meridiem == 'PM' else 0)
                time_from = f'{hour:02d}:{minute}'

            records.append({
                'title': title,
                'date': f'{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:]}',
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No classical ticketed performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PaducahSymphonySalesforceCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='paducahsymphony_my_salesforce_sites_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PaducahSymphonySalesforceCrawler().run()


if __name__ == '__main__':
    main()
