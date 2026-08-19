import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.pscny.org/'
SOURCE = "Peoples' Symphony Concerts"
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
LOCAL_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'Accept': 'application/json, text/html, application/xhtml+xml',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def event_urls_from_sitemap(xml):
    soup = BeautifulSoup(xml, 'xml')
    urls = []
    for location in soup.find_all('loc'):
        url = location.get_text(strip=True)
        if re.fullmatch(r'https://www\.pscny\.org/calendar/\d{4}/.+', url):
            urls.append(url)
    return sorted(set(urls))


def city_from_location(location):
    address_line = clean_text((location or {}).get('addressLine2'))
    if not address_line:
        return None
    city = address_line.split(',', 1)[0].strip()
    return city or None


def detail_page_url(item):
    body = item.get('body') or item.get('excerpt') or ''
    soup = BeautifulSoup(body, 'html.parser')
    for link in soup.find_all('a', href=True):
        if clean_text(link.get_text()).casefold() == 'more info':
            url = urljoin(SOURCE_URL, link['href'])
            if url.startswith(SOURCE_URL) and '/calendar/' not in url:
                return url
    return None


def description_from_item(item, session):
    summary = clean_text(item.get('body') or item.get('excerpt'))
    more_info_url = detail_page_url(item)
    if not more_info_url:
        return summary or None

    try:
        response = session.get(
            more_info_url,
            params={'format': 'json'},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        detail = clean_text(response.json().get('mainContent'))
        return detail or summary or None
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Could not load PSC concert programme page',
            event='crawler_detail_failed',
            level='warning',
            url=more_info_url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return summary or None


def record_from_item(item, event_url, session):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    start_timestamp = item.get('startDate')
    if (
        not title
        or not venue
        or venue.casefold() in {'tba', 'tbd', 'to be announced', 'to be determined'}
        or not city
        or not isinstance(start_timestamp, (int, float))
    ):
        return None

    try:
        starts_at = datetime.fromtimestamp(start_timestamp / 1000, tz=LOCAL_TIMEZONE)
    except (ValueError, OSError, OverflowError):
        return None

    canonical_url = urljoin(SOURCE_URL, item.get('fullUrl') or event_url)
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': canonical_url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description_from_item(item, session),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(SITEMAP_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    event_urls = event_urls_from_sitemap(response.text)

    records = []
    for event_url in event_urls:
        try:
            response = session.get(
                event_url,
                params={'format': 'json'},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            item = response.json().get('item')
            if not isinstance(item, dict):
                continue
            record = record_from_item(item, event_url, session)
            if record:
                records.append(record)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Could not load PSC calendar event',
                event='crawler_event_failed',
                level='warning',
                url=event_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    records.sort(key=lambda row: (row['date'], row['time_from'], row['title']))
    if not records:
        log_message(
            'No PSC calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return records


class PscnyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pscny_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
    PscnyOrgCrawler().run()


if __name__ == '__main__':
    main()
