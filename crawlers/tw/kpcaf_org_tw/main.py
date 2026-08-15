import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kpcaf.org.tw/'
LISTING_URL = urljoin(SOURCE_URL, 'index.php?temp=show&lang=cht')
SOURCE = 'Kaohsiung Philharmonic Cultural & Arts Foundation'
CITY = 'Kaohsiung'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_items(session):
    soup = get_soup(session, LISTING_URL)
    items = []
    seen = set()
    for card in soup.select('.blog-grid.blog-standard'):
        link = card.select_one('h5 a[href*="task=pageinfo"]')
        if not link:
            continue
        url = urljoin(SOURCE_URL, link.get('href'))
        if url in seen:
            continue
        seen.add(url)
        items.append({'url': url, 'title': clean_text(link), 'summary': clean_text(card)})
    return items


def parse_metadata(text):
    match = re.search(
        r'時間\s*[:：]\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})'
        r'(?:\s*\([^)]*\))?\s*(\d{1,2}:\d{2})?.*?'
        r'地點\s*[:：]\s*([^\n|]+)',
        text,
        re.S,
    )
    if not match:
        return None
    try:
        event_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    venue = clean_text(match.group(5)).strip(' |')
    time_from = match.group(4)
    return event_date.isoformat(), time_from, venue


def venue_city(venue):
    # The listing includes touring appearances. Only infer Kaohsiung for venues
    # whose names explicitly identify the city or well-known municipal halls.
    kaohsiung_markers = ('高雄', '衛武營', '大東文化藝術中心')
    if any(marker in venue for marker in kaohsiung_markers):
        return CITY
    return None


def parse_detail(session, item):
    soup = get_soup(session, item['url'])
    title = clean_text(soup.select_one('main h1.pagetitle')) or item['title']
    metadata = parse_metadata(clean_text(soup.select_one('main .post-meta')) or item['summary'])
    if not title or not metadata:
        return None
    event_date, time_from, venue = metadata
    city = venue_city(venue)
    if not venue or not city:
        return None

    description = clean_text(soup.select_one('main article.blog-post')) or None
    return {
        'title': title,
        'date': event_date,
        'url': item['url'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'TW',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_detail, session, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class KpcafOrgTwCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kpcaf_org_tw',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='TW',
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
        return get_concerts()


def main():
    KpcafOrgTwCrawler().run()


if __name__ == '__main__':
    main()
