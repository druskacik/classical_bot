import re
from datetime import UTC, datetime
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chorusofwesterly.org/'
EVENTS_URL = f'{SOURCE_URL}events'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'The Chorus of Westerly'
COUNTRY_CODE = 'US'
CITY = 'Westerly'
KENT_HALL = 'The George Kent Performance Hall'
LOCAL_TIMEZONE = ZoneInfo('America/New_York')

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
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ').replace('\u200b', ' ')).strip()


def parse_wix_datetime(value):
    """Convert the Wix CMS UTC display value to Westerly local date and time."""
    try:
        utc_value = datetime.strptime(clean_text(value), '%m/%d/%y, %I:%M %p').replace(
            tzinfo=UTC
        )
    except ValueError:
        return None
    local_value = utc_value.astimezone(LOCAL_TIMEZONE)
    return local_value.date().isoformat(), local_value.strftime('%H:%M')


def normalize_title(value):
    return re.sub(r'[^a-z0-9]+', '', clean_text(value).lower())


def detail_urls(session):
    try:
        response = session.get(SITEMAP_URL, timeout=45)
        response.raise_for_status()
        index = BeautifulSoup(response.text, 'xml')
        detail_sitemap_url = next(
            clean_text(node.get_text())
            for node in index.find_all('loc')
            if 'dynamic-master-events_' in clean_text(node.get_text())
        )
        response = session.get(detail_sitemap_url, timeout=45)
        response.raise_for_status()
    except (requests.RequestException, StopIteration) as error:
        log_message(
            'Event detail sitemap request failed',
            event='crawler_detail_sitemap_failed',
            level='warning',
            url=SITEMAP_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return {}

    soup = BeautifulSoup(response.text, 'xml')
    urls = {}
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        marker = '/master-events/'
        if marker not in url:
            continue
        slug = unquote(url.split(marker, 1)[1]).replace('-', ' ')
        urls[normalize_title(slug)] = url
    return urls


def venue_for(title):
    normalized = normalize_title(title)
    if normalized == 'speakeasychoir':
        return 'The United Theatre Black Box'
    if normalized.startswith('summerpops'):
        return 'Wilcox Park'
    return KENT_HALL


def parse_card(card, urls):
    fields = {
        node.get('id', '').split('__', 1)[0]: clean_text(node.get_text(' ', strip=True))
        for node in card.select('[data-testid="richTextElement"]')
    }
    title = fields.get('comp-kyykslu0', '')
    parsed_datetime = parse_wix_datetime(fields.get('comp-ldanqlx9', ''))
    description = fields.get('comp-kyykslu31') or None
    if not title or not parsed_datetime:
        return None

    event_date, time_from = parsed_datetime
    url = urls.get(normalize_title(title), EVENTS_URL)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue_for(title),
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = detail_urls(session)

    records = []
    for card in soup.select('.wixui-repeater__item'):
        record = parse_card(card, urls)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No event cards found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ChorusOfWesterlyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chorusofwesterly_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    ChorusOfWesterlyOrgCrawler().run()


if __name__ == '__main__':
    main()
