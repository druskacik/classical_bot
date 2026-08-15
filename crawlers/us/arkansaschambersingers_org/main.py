import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://arkansaschambersingers.org/'
SOURCE = 'Arkansas Chamber Singers'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
PAST_EVENTS_URL = urljoin(
    SOURCE_URL,
    'calendar/features/load/calendar_feature_1378788?calendar_page_prev=1',
)

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
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for link in soup.select('a.event_details[href*="/go/events/"]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        if url not in links:
            links.append(url)
    return links


def event_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'MusicEvent':
                return candidate
    return {}


def parse_country_and_city(address):
    address = clean_text(address)
    if not address:
        return '', ''

    if re.search(r'\bAustria\b', address, re.I):
        country_code = 'AT'
    elif re.search(r'\b(?:Arkansas|AR)(?:\s+\d{5}(?:-\d{4})?)?\b', address, re.I):
        country_code = 'US'
    else:
        return '', ''

    parts = [part.strip() for part in address.split(',') if part.strip()]
    if len(parts) < 2:
        return country_code, ''
    city = re.sub(
        r'^(?:Austria|Arkansas|AR)(?:\s+\d{5}(?:-\d{4})?)?$',
        '',
        parts[-1],
        flags=re.I,
    )
    if not city:
        city = parts[-2]
    if re.search(r'\s[&/]\s|\band\b', city, re.I):
        city = ''
    return country_code, clean_text(city)


def parse_event(html):
    soup = BeautifulSoup(html, 'html.parser')
    data = event_json_ld(soup)
    location = data.get('location') if isinstance(data.get('location'), dict) else {}
    title = clean_text(data.get('name'))
    url = clean_text(data.get('url'))
    start = clean_text(data.get('startDate'))
    venue = clean_text(location.get('name'))
    address = location.get('address')
    if isinstance(address, dict):
        address = ', '.join(
            clean_text(address.get(field))
            for field in ('streetAddress', 'addressLocality', 'addressRegion', 'postalCode')
            if clean_text(address.get(field))
        )
    country_code, city = parse_country_and_city(address)

    try:
        start_datetime = datetime.fromisoformat(start.replace('Z', '+00:00'))
        event_date = start_datetime.date().isoformat()
        time_from = start_datetime.strftime('%H:%M') if 'T' in start else None
    except ValueError:
        event_date = ''
        time_from = None

    notes = soup.select_one('.event-notes')
    description = clean_text(notes) or None
    if not all((title, event_date, url, venue, city, country_code)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ArkansasChamberSingersOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='arkansaschambersingers_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        links = []
        for listing_url in (CALENDAR_URL, PAST_EVENTS_URL):
            request_headers = {}
            if listing_url == PAST_EVENTS_URL:
                request_headers = {
                    'Accept': 'text/vnd.turbo-stream.html',
                    'Referer': CALENDAR_URL,
                }
            response = session.get(listing_url, headers=request_headers, timeout=45)
            response.raise_for_status()
            for link in event_links(response.text):
                if link not in links:
                    links.append(link)

        records = []
        for detail_url in links:
            try:
                response = session.get(detail_url, timeout=45)
                response.raise_for_status()
                record = parse_event(response.text)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Arkansas Chamber Singers event',
                    event='crawler_item_failed',
                    level='warning',
                    url=detail_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Arkansas Chamber Singers event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=detail_url,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, city, or country is missing',
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    ArkansasChamberSingersOrgCrawler().run()


if __name__ == '__main__':
    main()
