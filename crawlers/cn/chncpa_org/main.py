import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chncpa.org/'
SOURCE = 'National Centre for the Performing Arts'
DATES_URL = f'{SOURCE_URL}djyrl/dateycgp09.html'
DETAIL_URL = 'https://openapi.chncpa.org/product/detail'
PRODUCT_URL = 'https://wticket.chncpa.org/product.html?id={product_id}'
CITY = 'Beijing'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Referer': SOURCE_URL,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u3000', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def product_ids(session):
    # This calendar feed is the site's source of truth for selectable dates. It
    # includes both its retained past dates and all currently published future
    # performances, and gives us product IDs without issuing one query per day.
    entries = get_json(session, DATES_URL)
    return sorted({str(item.get('PRODUCTID')) for item in entries if item.get('PRODUCTID')})


def detail_description(detail):
    parts = []
    summary = clean_text(detail.get('productPageDescription'))
    if summary:
        parts.append(summary)

    # Types 190 and 200 are generic ticketing rules and audience etiquette.
    # Other sections contain synopses, artist notes, and (type 70) programmes.
    for section in detail.get('introduce') or []:
        if str(section.get('type')) in {'190', '200'}:
            continue
        heading = clean_text(section.get('title'))
        section_parts = []
        for content in section.get('contents') or []:
            body = clean_text(content.get('content'))
            if body and body not in section_parts:
                section_parts.append(body)
        if section_parts:
            value = '\n'.join(section_parts)
            parts.append(f'{heading}\n{value}' if heading else value)
    return clean_text('\n\n'.join(parts)) or None


def make_records(product_id, payload):
    if str(payload.get('code')) != '0' or not isinstance(payload.get('data'), dict):
        return []
    detail = payload['data']
    title = clean_text(detail.get('productName'))
    venue = clean_text(detail.get('venueName'))
    address = clean_text(detail.get('venueAddress'))
    url = PRODUCT_URL.format(product_id=product_id)

    # Every venue in this NCPA calendar is operated in Beijing. The API's
    # explicit Beijing address guards against applying that default to a tour.
    if not title or not venue or '北京' not in address:
        return []

    description = detail_description(detail)
    records = []
    for session in detail.get('calendar') or []:
        match = re.match(r'^(\d{4}-\d{2}-\d{2})(?:\s+\S+)?\s+(\d{2}:\d{2})$', session.get('sessionDate') or '')
        if not match:
            continue
        try:
            event_date = date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': match.group(2),
            'venue': venue,
            'city': CITY,
            'country_code': 'CN',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    ids = product_ids(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_json, session, DETAIL_URL, {'productId': product_id, 'channel': 'pc'}): product_id
            for product_id in ids
        }
        for future in as_completed(futures):
            product_id = futures[future]
            try:
                records.extend(make_records(product_id, future.result()))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=PRODUCT_URL.format(product_id=product_id),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title'], item['url']))


class ChncpaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chncpa_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CN',
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
    ChncpaOrgCrawler().run()


if __name__ == '__main__':
    main()
