import re
import warnings
from datetime import datetime, timedelta

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.exceptions import SSLError

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.valdostasymphony.org/'
TICKETS_URL = f'{SOURCE_URL}tickets.php'
SOURCE = 'Valdosta Symphony Orchestra'
COUNTRY_CODE = 'US'
SITE_ID = '39AE1A56-20BE-429E-B5BC-408EB358B29C'
PLUGIN_URL = 'https://plugin.vbotickets.com'

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
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    value = re.sub(r'(?i)(\d)([ap]m)$', r'\1 \2', value.strip())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value.strip().upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_event_dates(value):
    text = clean_text(value)
    match = re.search(
        r'(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*)?'
        r'(\d{1,2}/\d{1,2}/\d{4})'
        r'(?:\s*-\s*(\d{1,2}/\d{1,2}/\d{4}))?'
        r'(?:\s*@\s*(\d{1,2}(?::\d{2})?\s*[AP]M))?',
        text,
        re.IGNORECASE,
    )
    if not match:
        return []

    try:
        start = datetime.strptime(match.group(1), '%m/%d/%Y').date()
        end = datetime.strptime(match.group(2), '%m/%d/%Y').date() if match.group(2) else start
    except ValueError:
        return []
    if end < start or (end - start).days > 14:
        return []

    time_from = parse_time(match.group(3)) if match.group(3) else None
    return [
        ((start + timedelta(days=offset)).isoformat(), time_from)
        for offset in range((end - start).days + 1)
    ]


def plugin_session_id(session):
    params = {
        'siteid': SITE_ID,
        'page': 'ListEvents',
        'w': '1280',
        'h': '720',
        'o': '0',
        'parent': 'www.valdostasymphony.org',
        'parenturl': TICKETS_URL,
        'PluginType': '',
    }
    response = session.get(f'{PLUGIN_URL}/plugin/loadplugin', params=params, timeout=45)
    response.raise_for_status()
    match = re.search(r'[?&]s=([0-9a-f-]{36})', response.text, re.IGNORECASE)
    if not match:
        raise ValueError('VBO Tickets did not provide a plugin session ID')
    return match.group(1)


def parse_plugin_event(card):
    title = clean_text(card.select_one('.HeaderEventName'))
    date_values = parse_event_dates(card.select_one('.TextEventDate'))
    venue_node = card.select_one('.TextVenueName')
    if not title or not date_values or not venue_node:
        return []

    venue = clean_text(next(venue_node.children, '')).strip()
    location = clean_text(venue_node)
    city_match = re.search(r',\s*([^,\n]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?', location)
    city = city_match.group(1).strip() if city_match else ''
    if not venue or not city:
        return []

    event_id = card.get('id', '').removeprefix('EID')
    if not event_id.isdigit():
        return []
    description = clean_text(card.select_one('.EventIntroText')) or None
    return [
        {
            'title': title,
            'date': event_date,
            'url': TICKETS_URL,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in date_values
    ]


def scrape_plugin(session):
    session_id = plugin_session_id(session)
    records = []
    for event_type in ('current', 'past'):
        response = session.get(
            f'{PLUGIN_URL}/Plugin/events/showevents',
            params={
                'ViewType': 'grid',
                'EventType': event_type,
                'day': '',
                's': session_id,
            },
            timeout=45,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        cards = soup.select('.EventGridItem')
        log_message(
            'VBO Tickets feed fetched',
            event='crawler_listing_fetched',
            url=response.url,
            event_type=event_type,
            record_count=len(cards),
        )
        for card in cards:
            records.extend(parse_plugin_event(card))
    return records


def scrape_homepage(session):
    try:
        response = session.get(SOURCE_URL, timeout=45)
    except SSLError as error:
        log_message(
            'Retrying homepage because its certificate chain could not be verified',
            event='crawler_tls_verification_failed',
            level='warning',
            url=SOURCE_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', urllib3.exceptions.InsecureRequestWarning)
            response = session.get(SOURCE_URL, timeout=45, verify=False)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    date_re = re.compile(
        r'([A-Z][a-z]+ \d{1,2}, \d{4})\s+at\s+'
        r'(\d{1,2}(?::\d{2})?\s*[ap]m)\s*-\s*(.+)',
        re.IGNORECASE,
    )
    for text_node in soup.find_all(string=lambda text: text and date_re.search(text)):
        match = date_re.search(clean_text(text_node))
        container = text_node.find_parent(class_='about_taital_main')
        if not match or not container:
            continue
        try:
            event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        except ValueError:
            continue
        venue = clean_text(match.group(3)).strip(' -')
        title = clean_text(container.select_one('.about_taital'))
        description_node = container.select_one('p.about_text.nunito')
        description = clean_text(description_node) or None
        if title and venue:
            records.append({
                'title': title,
                'date': event_date,
                'url': TICKETS_URL,
                'time_from': parse_time(match.group(2)),
                'venue': venue,
                'city': 'Valdosta',
                'country_code': COUNTRY_CODE,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = scrape_plugin(session)
    records.extend(scrape_homepage(session))
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ValdostaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='valdostasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    ValdostaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
