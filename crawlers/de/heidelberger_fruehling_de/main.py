import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.heidelberger-fruehling.de/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/hdfevent'
SOURCE = 'Heidelberger Frühling'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_event_pages(session):
    pages = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API,
            params={
                'page': page,
                'per_page': 100,
                'orderby': 'date',
                'order': 'asc',
                '_fields': 'id,link,title',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return pages
        page += 1


def parse_date_and_time(text):
    match = re.search(
        r'\b(?:Mo|Di|Mi|Do|Fr|Sa|So)?\s*(\d{1,2})\.\s*'
        r'(\d{1,2})\.\s*(\d{4})(?:.*?\b(\d{1,2})[.:](\d{2})\s*Uhr)?',
        text,
        re.DOTALL,
    )
    if not match:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), int(match.group(2)), int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        hour, minute = int(match.group(4)), int(match.group(5))
        if hour < 24 and minute < 60:
            time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def find_event_header(article):
    for element in article.select('div'):
        classes = element.get('class') or []
        if 'lg:grid-cols-5' in classes and 'font-custom' in classes:
            return element
    return None


def find_venue(header):
    link = header.select_one('a[href*="/spielstaetten/"]')
    if link:
        return clean_text(link.get_text(' ', strip=True))
    for element in header.find_all('div', recursive=False):
        classes = element.get('class') or []
        if 'order-5' in classes:
            return clean_text(element.get_text(' ', strip=True))
    return ''


def find_city(article):
    location = article.select_one('section.py-section-meta')
    if location:
        text = clean_text(location.get_text('\n', strip=True))
        match = re.search(r'\b\d{5}\s+([^\n,]+)', text)
        if match:
            return clean_text(match.group(1))

    # Published event pages are based in Heidelberg unless their location
    # block explicitly identifies another city. This fallback covers older
    # pages whose venue entry omits its street address.
    header = find_event_header(article)
    header_text = clean_text(header.get_text(' ', strip=True)) if header else ''
    return 'Heidelberg' if 'Heidelberg' in header_text else ''


def find_description(article):
    flexible = article.select_one('.flexible-content')
    if not flexible:
        return None
    parts = []
    for section in flexible.select(':scope > section'):
        classes = set(section.get('class') or [])
        if classes.intersection(
            {'py-section-meta', 'pt-section-carousel', 'wrap--sponsors'}
        ):
            continue
        text = clean_text(section.get_text('\n', strip=True))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(page, markup):
    soup = BeautifulSoup(markup, 'html.parser')
    article = soup.select_one('article.hdfevent')
    if not article:
        return None

    title_node = article.select_one('h2.single-title')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    if not title:
        title = clean_text((page.get('title') or {}).get('rendered'))

    header = find_event_header(article)
    if not header:
        return None
    event_date, time_from = parse_date_and_time(
        clean_text(header.get_text('\n', strip=True))
    )
    venue = find_venue(header)
    city = find_city(article)
    url = page.get('link') or ''
    if not title or not event_date or not url or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': find_description(article),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    pages = get_event_pages(session)
    records = []

    def fetch(page):
        response = session.get(page['link'], timeout=45)
        response.raise_for_status()
        return parse_event(page, response.text)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch, page): page for page in pages}
        for future in as_completed(futures):
            page = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=page.get('link'),
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


class HeidelbergerFruehlingDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='heidelberger_fruehling_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        return get_concerts()


def main():
    HeidelbergerFruehlingDeCrawler().run()


if __name__ == '__main__':
    main()
