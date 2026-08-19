import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oaklandsymphony.org/'
SOURCE = 'Oakland Symphony'
PAST_EVENTS_URL = urljoin(SOURCE_URL, 'concerts/past-events/')
CURRENT_FEEDS = [
    urljoin(SOURCE_URL, 'event-category/classical-series/'),
    urljoin(SOURCE_URL, 'event-category/chorus-concerts/'),
    urljoin(SOURCE_URL, 'event-category/youth-orchestra-concerts/'),
    urljoin(SOURCE_URL, 'event-category/tickets-available/'),
]

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = clean_text(value)
    value = re.sub(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*', '', value, flags=re.I)
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).lower().replace('.', '')
    range_match = re.search(
        r'\b(\d{1,2})(?::(\d{2}))?\s*(?:[ap]m)?\s*-\s*'
        r'\d{1,2}(?::\d{2})?\s*([ap]m)\b',
        value,
    )
    if range_match:
        hour, minute, meridiem = range_match.groups()
        try:
            return datetime.strptime(
                f'{hour}:{minute or "00"} {meridiem}', '%I:%M %p'
            ).strftime('%H:%M')
        except ValueError:
            return None
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b', value)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    try:
        return datetime.strptime(
            f'{hour}:{minute or "00"} {meridiem}', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None


def event_urls_from_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.select('main a[href*="/event/"]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        if re.match(r'^https://www\.oaklandsymphony\.org/event/[^/]+/?$', url):
            urls.append(url if url.endswith('/') else f'{url}/')
    return list(dict.fromkeys(urls))


def spec_value(detail, label):
    for row in detail.select('p.spec'):
        key = row.select_one('.left')
        value = row.select_one('.right')
        if key and value and clean_text(key).rstrip(':').lower() == label.lower():
            return clean_text(value), value
    return '', None


def city_from_venue_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    main_text = clean_text(soup.select_one('main'))
    match = re.search(
        r'(?:^|\n)[^\n,]+(?:\n|,\s*)[^\n,]+,\s*([A-Za-z .\'-]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?',
        main_text,
        re.MULTILINE,
    )
    return clean_text(match.group(1)) if match else ''


def parse_event(html, url, session, venue_city_cache):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1'))
    detail = soup.select_one('main .event-detail')
    if not title or not detail:
        return None

    date_text, _ = spec_value(detail, 'date')
    time_text, _ = spec_value(detail, 'time')
    venue, venue_node = spec_value(detail, 'location')
    event_date = parse_date(date_text)
    if not event_date or not venue:
        return None

    venue_url = None
    if venue_node:
        link = venue_node.find('a', href=True)
        if link:
            venue_url = urljoin(url, link['href'])

    city = venue_city_cache.get(venue_url, '') if venue_url else ''
    if venue_url and venue_url not in venue_city_cache:
        try:
            response = session.get(venue_url, timeout=45)
            response.raise_for_status()
            city = city_from_venue_page(response.text)
        except requests.RequestException as error:
            log_message(
                'Venue page request failed',
                event='crawler_venue_request_failed',
                level='warning',
                url=venue_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            city = ''
        venue_city_cache[venue_url] = city

    if not city:
        return None

    program = detail.select_one('#program-snippet')
    description = clean_text(program)
    description = re.sub(r'^Program:\s*', '', description, flags=re.I).strip() or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_text),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    event_urls = []
    for listing_url in [*CURRENT_FEEDS, PAST_EVENTS_URL]:
        try:
            response = session.get(listing_url, timeout=45)
            response.raise_for_status()
            event_urls.extend(event_urls_from_listing(response.text))
        except requests.RequestException as error:
            log_message(
                'Event listing request failed',
                event='crawler_listing_request_failed',
                level='warning',
                url=listing_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    records = []
    venue_city_cache = {}
    for url in dict.fromkeys(event_urls):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_event(response.text, url, session, venue_city_cache)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=PAST_EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OaklandSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oaklandsymphony_org',
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
    OaklandSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
