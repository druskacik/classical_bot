import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://festspiele-mv.de/'
LIST_URL = urljoin(SOURCE_URL, 'alle-konzerte')
SOURCE = 'Festspiele Mecklenburg-Vorpommern'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_items(session):
    # The unfiltered calendar is the site's complete currently published feed.
    # Its date controls do not expose an archive: past date values are ignored.
    soup = get_soup(session, LIST_URL)
    items = list(soup.select('#programListContainer li[data-uid]'))

    more = soup.select_one('a.load-more[data-url]')
    if more:
        total_pages = int(more.get('data-total-pgs', '1'))
        page_url = urljoin(SOURCE_URL, more['data-url'])
        for page_number in range(2, total_pages + 1):
            page_soup = get_soup(session, page_url)
            items.extend(page_soup.select('li[data-uid]'))
            next_page = page_soup.select_one('#listAjaxUrl[data-url]')
            if page_number < total_pages and not next_page:
                break
            if next_page:
                page_url = urljoin(SOURCE_URL, next_page['data-url'].strip())

    return items


def parse_listing_item(item):
    link = item.select_one('a[href*="/konzerte/program/"]')
    time_tag = item.select_one('time[datetime]')
    title_node = item.select_one('.type-of-concert')
    location_node = item.select_one('.location')
    if not all((link, time_tag, title_node, location_node)):
        return None

    try:
        starts_at = datetime.fromisoformat(time_tag['datetime'].strip())
    except (KeyError, TypeError, ValueError):
        return None

    location = clean_text(location_node)
    if ',' not in location:
        return None
    city, venue = (part.strip() for part in location.split(',', 1))
    title = clean_text(title_node)
    url = urljoin(SOURCE_URL, link.get('href', ''))
    if not all((title, url, city, venue)):
        return None

    context = [clean_text(item.select_one(selector)) for selector in ('.program-name', '.other-data')]
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': '\n'.join(value for value in context if value) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(soup, fallback=None):
    parts = []
    for selector in ('.program-artists_and_tips .aside', '.program-artists_and_tips .article', '.rte'):
        for node in soup.select(selector):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or fallback


def enrich_record(session, record):
    soup = get_soup(session, record['url'])
    record['description'] = detail_description(soup, record['description'])
    return record


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = [record for item in listing_items(session) if (record := parse_listing_item(item))]

    enriched = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(enrich_record, session, record): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                enriched.append(record)

    return sorted(
        enriched,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class FestspieleMvDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festspiele_mv_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FestspieleMvDeCrawler().run()


if __name__ == '__main__':
    main()
