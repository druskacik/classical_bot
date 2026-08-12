import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rcm.ac.uk/'
EVENTS_URL = urljoin(SOURCE_URL, 'events/')
SOURCE = 'Royal College of Music'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# These are RCM/nearby London halls for which the institution and venue name
# provide strong location evidence. Touring venues are handled separately.
LONDON_VENUE_MARKERS = (
    'amaryllis fleming', 'britten theatre', 'performance hall', 'recital hall',
    'parry rooms', 'east parry room', 'west parry room', 'royal albert hall',
    "st james's piccadilly", 'st james piccadilly', 'st pancras church',
    'westminster abbey', 'south kensington', 'royal college of music',
    'rcm museum', 'rcm foyer', 'carne room', 'performance studio',
    'weston discovery centre', 'austrian cultural forum', 'sw7 ',
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def listing_urls(session):
    # The server accepts historical dates even though the browser date picker
    # prevents selecting them. A wide range returns the complete retained feed.
    response = get_response(
        session,
        EVENTS_URL,
        params={'datefrom': '2000-01-01', 'dateto': '2100-12-31', 'filters': '1'},
    )
    soup = BeautifulSoup(response.content, 'html.parser')
    urls = []
    for card in soup.select('div.Listing'):
        event_types = {
            clean_text(link)
            for link in card.select('a.tag-bubble[href*="eventtype="]')
        }
        # Museum visits are concrete events but are unambiguously out of scope.
        if event_types == {'Visit'}:
            continue
        link = card.select_one('a[href*="/events/details/?id="]')
        if link:
            urls.append(urljoin(SOURCE_URL, link.get('href')))
    return list(dict.fromkeys(urls))


def structured_event(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get('@type') == 'Event':
            return value
    return None


def infer_city(venue):
    lowered = venue.lower()
    if 'aylesbury' in lowered:
        return 'Aylesbury'
    if 'chichester' in lowered or 'pallant house' in lowered:
        return 'Chichester'
    if 'dorchester' in lowered:
        return 'Dorchester'
    if 'london' in lowered or any(marker in lowered for marker in LONDON_VENUE_MARKERS):
        return 'London'
    return None


def detail_description(soup):
    content = soup.select_one('.main-page-content')
    if not content:
        return None
    for node in content.select(
        '.hide-for-medium, .special-offer, .tag-bubble, .tag-bubble-span, '
        '.quotes-gallery, script, style, button'
    ):
        node.decompose()
    parts = []
    for node in content.select('p, #description'):
        if node.find_parent('p') or (node.get('id') != 'description' and node.find_parent(id='description')):
            continue
        text = clean_text(node)
        if not text or text in parts:
            continue
        if re.match(r'^(?:venue|tickets?|book|location|transport|food|access)\b', text, re.I):
            continue
        parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    data = structured_event(soup)
    if not data:
        return None
    title = clean_text(data.get('name'))
    venue_data = data.get('location') or {}
    venue = clean_text(venue_data.get('name') if isinstance(venue_data, dict) else venue_data)
    city = infer_city(venue)
    try:
        start = datetime.fromisoformat(str(data.get('startDate')).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if not title or not venue or not city:
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': detail_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
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
                    'Failed to scrape RCM event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class RcmAcUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rcm_ac_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    RcmAcUkCrawler().run()


if __name__ == '__main__':
    main()
