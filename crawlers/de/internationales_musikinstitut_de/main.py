import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://internationales-musikinstitut.de/de/imd/'
PROGRAM_URL = 'https://internationales-musikinstitut.de/de/ferienkurse/festival/programm/'
CONCERTS_URL = f'{PROGRAM_URL}filter/konzert/'
SOURCE = 'Internationales Musikinstitut Darmstadt'
CITY = 'Darmstadt'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}
DATE_PATTERN = re.compile(
    r'(\d{1,2})\.\s*'
    r'(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s*'
    r'(\d{4})(?:,\s*(\d{1,2}):(\d{2}))?'
)
MONTHS = {
    'Januar': 1,
    'Februar': 2,
    'März': 3,
    'April': 4,
    'Mai': 5,
    'Juni': 6,
    'Juli': 7,
    'August': 8,
    'September': 9,
    'Oktober': 10,
    'November': 11,
    'Dezember': 12,
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_items():
    soup = get_soup(CONCERTS_URL)
    items = []
    for article in soup.select('article.archive_day_entry'):
        link = article.select_one('a[href]')
        title_node = article.select_one('.list-item_title')
        meta = article.select_one('.list-item_meta')
        if not link or not title_node or not meta:
            continue
        items.append(
            {
                'url': link.get('href', '').strip(),
                'title': clean_text(title_node),
                'subtitle': clean_text(article.select_one('.list-item_subtitle')),
                'meta': clean_text(meta),
            }
        )
    return items


def parse_detail(item):
    soup = get_soup(item['url'])
    title = clean_text(soup.select_one('.post-header_title')) or item['title']
    date_text = clean_text(soup.select_one('.event_date'))
    match = DATE_PATTERN.search(date_text)
    venue = clean_text(soup.select_one('.event_location'))
    if not title or not match or not venue:
        return None

    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except (ValueError, KeyError):
        return None

    time_from = None
    if match.group(4):
        time_from = f'{int(match.group(4)):02d}:{match.group(5)}'

    description_parts = []
    if item['subtitle']:
        description_parts.append(item['subtitle'])
    for node in soup.select('.single_main .module-rich-text .rich-text'):
        text = clean_text(node)
        if not text or 'Veranstaltung hat bereits stattgefunden' in text:
            continue
        if text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': item['url'],
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    items = listing_items()
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_detail, item): item for item in items}
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


class InternationalesMusikinstitutDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='internationales_musikinstitut_de',
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
    InternationalesMusikinstitutDeCrawler().run()


if __name__ == '__main__':
    main()
