import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.synchromy.org/'
SOURCE = 'Synchromy'

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


def eventbrite_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.select('main a[href*="eventbrite.com/e/"]'):
        url = (link.get('href') or '').split('?')[0]
        if url.startswith('https://www.eventbrite.com/e/') and url not in urls:
            urls.append(url)
    return urls


def event_data(html):
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one('script#__NEXT_DATA__')
    if not node or not node.string:
        return None
    try:
        context = json.loads(node.string)['props']['pageProps']['context']
        basic = context['basicInfo']
    except (KeyError, TypeError, json.JSONDecodeError):
        return None

    venue_data = basic.get('venue') or {}
    address = venue_data.get('address') or {}
    start = (basic.get('startDate') or {}).get('local', '')
    try:
        start_at = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None

    description_parts = []
    summary = clean_text(basic.get('summary'))
    if summary:
        description_parts.append(summary)
    for module in (context.get('structuredContent') or {}).get('modules') or []:
        text = clean_text(module.get('text')) if module.get('type') == 'text' else ''
        if text and text not in description_parts:
            description_parts.append(text)

    title = clean_text(basic.get('name'))
    venue = clean_text(venue_data.get('name'))
    city = clean_text(address.get('city'))
    country_code = clean_text(address.get('country')).upper()
    url = (basic.get('url') or '').split('?')[0]
    if not all((title, venue, city, url)) or country_code != 'US':
        return None

    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    urls = eventbrite_urls(response.text)
    if not urls:
        log_message(
            'No linked events found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
        return []

    records = []
    for url in urls:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            record = event_data(detail_response.text)
            if record:
                records.append(record)
            else:
                log_message(
                    'Event detail could not be parsed',
                    event='crawler_detail_skipped',
                    level='warning',
                    url=url,
                    error_type='ParseError',
                )
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class SynchromyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='synchromy_org',
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
    SynchromyOrgCrawler().run()


if __name__ == '__main__':
    main()
