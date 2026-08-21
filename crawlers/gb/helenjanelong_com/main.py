import html
import json
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://helenjanelong.com/'
SOURCE = 'Helen Jane Long'
EVENT_SITEMAP_URL = 'https://helenjanelong.com/tribe_events-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'de': 'DE',
    'germany': 'DE',
    'gb': 'GB',
    'great britain': 'GB',
    'uk': 'GB',
    'united kingdom': 'GB',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    elif not isinstance(value, str):
        value = str(value)
    value = html.unescape(value)
    if '<' in value and '>' in value:
        value = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    value = value.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def iter_json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def find_event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in iter_json_objects(data):
            item_type = item.get('@type')
            if item_type == 'Event' or isinstance(item_type, list) and 'Event' in item_type:
                return item
    return None


def country_code(value):
    value = clean_text(value)
    if not value:
        return None
    if re.fullmatch(r'[A-Za-z]{2}', value):
        return value.upper()
    return COUNTRY_CODES.get(value.casefold())


def parse_event_page(page_url, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    event = find_event_data(soup)
    if not event:
        return None

    location = event.get('location') or {}
    address = location.get('address') or {}
    if isinstance(address, str):
        address = {}
    start = clean_text(event.get('startDate'))
    title = clean_text(event.get('name'))
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    event_country = country_code(address.get('addressCountry'))
    if not all((title, start, venue, city, event_country)):
        return None

    date_match = re.match(r'^(\d{4}-\d{2}-\d{2})', start)
    time_match = re.search(r'T(\d{2}:\d{2})', start)
    if not date_match:
        return None
    try:
        event_date = date.fromisoformat(date_match.group(1)).isoformat()
    except ValueError:
        return None

    description_element = soup.select_one(
        '.tribe-events-single-event-description, [data-testid="section-wrapper-overview"]'
    )
    description = clean_text(description_element) or clean_text(event.get('description')) or None

    return {
        'title': title,
        'date': event_date,
        'url': clean_text(event.get('url')) or page_url,
        'time_from': time_match.group(1) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': event_country,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def sitemap_event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    return [
        clean_text(location)
        for location in soup.find_all('loc')
        if '/event/' in clean_text(location)
    ]


def homepage_widget_urls(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    urls = []
    for link in soup.select('.wfea a[rel~="bookmark"][href], a[id^="wfea-popup-title-"][href]'):
        url = link.get('href', '').strip()
        if url.startswith(('http://', 'https://')):
            urls.append(url)
    return urls


class HelenJaneLongComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='helenjanelong_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            homepage_response = session.get(SOURCE_URL, timeout=45)
            homepage_response.raise_for_status()
            sitemap_response = session.get(EVENT_SITEMAP_URL, timeout=45)
            sitemap_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Helen Jane Long event indexes',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = sitemap_event_urls(sitemap_response.text)
        urls.extend(homepage_widget_urls(homepage_response.text))
        urls = list(dict.fromkeys(urls))

        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Helen Jane Long event',
                    event='crawler_event_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            record = parse_event_page(response.url, response.text)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped Helen Jane Long event without required structured fields',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    HelenJaneLongComCrawler().run()


if __name__ == '__main__':
    main()
