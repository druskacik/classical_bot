import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.delawaresymphony.org/'
TICKET_URL = 'https://delawaresymphony.my.salesforce-sites.com/ticket/'
SOURCE = 'Delaware Symphony Orchestra'
CONTROLLER = 'PatronTicket.Controller_PublicTicketApp'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'X-User-Agent': 'Visualforce-Remoting',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': TICKET_URL,
}

# Venue IDs are stable Salesforce record IDs. Unknown future venues are resolved
# from the event descriptor rather than guessed.
VENUES = {
    'a0TE000000alh6EMAQ': ('The Grand Opera House', 'Wilmington'),
    'a0TRd00000C9h10MAB': ('Westminster Presbyterian Church', 'Wilmington'),
    'a0T0L00000jsJLMUA2': ('Cape Henlopen High School Theatre', 'Lewes'),
    'a0T0L00000jrvfkUAA': ('Hotel Du Pont', 'Wilmington'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def remoting_config(html):
    match = re.search(r'RemotingProviderImpl\((\{.*?\})\)\);', html)
    if not match:
        raise ValueError('Visualforce remoting configuration was not found')
    return json.loads(match.group(1))


def remote_method(config, name):
    controller = config['actions'][CONTROLLER]
    return next(method for method in controller['ms'] if method['name'] == name)


def remote_call(session, config, method_name, data, tid=1):
    method = remote_method(config, method_name)
    body = {
        'action': CONTROLLER,
        'method': method_name,
        'data': data,
        'type': 'rpc',
        'tid': tid,
        'ctx': {
            'csrf': method['csrf'],
            'vid': config['vf']['vid'],
            'ns': method['ns'],
            'ver': int(method['ver']),
            'authorization': method['authorization'],
        },
    }
    response = session.post(
        f'{TICKET_URL}apexremote', json=body, headers=HEADERS, timeout=90
    )
    response.raise_for_status()
    payload = response.json()[0]
    if payload.get('statusCode') != 200:
        raise ValueError(f'Visualforce method {method_name} failed')
    return payload.get('result')


def extract_city(address):
    text = clean_text(address)
    match = re.search(r'(?:^|\n)([^,\n]+),\s*DE\s+\d{5}\b', text, re.I)
    return clean_text(match.group(1)) if match else ''


def resolve_venue(session, config, instance, cache):
    venue_id = instance.get('venueId')
    if venue_id in cache:
        return cache[venue_id]
    descriptor = remote_call(
        session, config, 'fetchEventDescriptor', [instance.get('id'), '', ''], tid=2
    )
    venue = (descriptor or {}).get('venue') or {}
    resolved = (clean_text(venue.get('name')), extract_city(venue.get('addressInfo')))
    if all(resolved) and venue_id:
        cache[venue_id] = resolved
    return resolved


def make_record(event, instance, venue, city):
    title = clean_text(event.get('name'))
    formatted = instance.get('formattedDates') or {}
    date_value = str(formatted.get('YYYYMMDD') or '')
    time_value = clean_text(formatted.get('TIME_STRING'))
    url = instance.get('purchaseUrl') or event.get('purchaseUrl') or ''

    try:
        event_date = datetime.strptime(date_value, '%Y%m%d').date().isoformat()
        time_from = datetime.strptime(time_value.upper(), '%I:%M %p').strftime('%H:%M')
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue or not city:
        return None
    description_parts = [
        clean_text(event.get('description')),
        clean_text(event.get('detail')),
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update({'User-Agent': HEADERS['User-Agent']})
    response = session.get(TICKET_URL, timeout=60)
    response.raise_for_status()
    config = remoting_config(response.text)
    events = remote_call(session, config, 'fetchEvents', [TICKET_URL, '', '', 'Expanded']) or []
    venues = dict(VENUES)
    records = []

    for event in events:
        # The feed also returns season/subscription overview products. Only
        # first-party objects explicitly typed as ticketed events are concerts.
        if event.get('type') != 'Tickets':
            continue
        for instance in event.get('instances') or []:
            try:
                venue, city = resolve_venue(session, config, instance, venues)
                record = make_record(event, instance, venue, city)
            except (KeyError, StopIteration, requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Delaware Symphony concert instance',
                    event='crawler_item_failed',
                    level='warning',
                    url=instance.get('purchaseUrl') or TICKET_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                record = None
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
    )


class DelawareSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='delawaresymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    DelawareSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
