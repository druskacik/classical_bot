import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hmt-leipzig.de/'
SOURCE = 'HMT Leipzig'
CURRENT_URL = urljoin(SOURCE_URL, 'veranstaltungen')
ARCHIVE_URL = urljoin(SOURCE_URL, 'veranstaltungen-archiv')
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
GERMAN_MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'dezember': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u00ad', '')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=12,
        pool_maxsize=12,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def add_month(year, month, offset):
    ordinal = year * 12 + month - 1 + offset
    return ordinal // 12, ordinal % 12 + 1


def calendar_url(base_url, year, month):
    return (
        f'{base_url}?tx_news_pi1%5BoverwriteDemand%5D%5Bmonth%5D={month}'
        f'&tx_news_pi1%5BoverwriteDemand%5D%5Byear%5D={year}'
    )


def parse_datetime(value):
    text = clean_text(value).casefold()
    match = re.search(
        r'(\d{1,2})\.\s*([a-zä]+)\s*(20\d{2})'
        r'(?:\s+(\d{1,2}):([0-5]\d)\s*uhr)?',
        text,
    )
    if not match or match.group(2) not in GERMAN_MONTHS:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), GERMAN_MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(4):
        hour = int(match.group(4))
        if hour < 24:
            event_time = f'{hour:02d}:{match.group(5)}'
    return event_date, event_time


def city_from_venue(venue):
    postal = re.search(r'\b\d{5}\s+([^,;\n]+)', venue)
    if postal:
        return postal.group(1).strip()
    folded = venue.casefold()
    known_places = {
        'bad lauchstädt': 'Goethestadt Bad Lauchstädt',
        'halle (saale)': 'Halle (Saale)',
        'markkleeberg': 'Markkleeberg',
        'schkeuditz': 'Schkeuditz',
        'delitzsch': 'Delitzsch',
        'torgau': 'Torgau',
        'grimma': 'Grimma',
        'dresden': 'Dresden',
        'berlin': 'Berlin',
    }
    for marker, city in known_places.items():
        if marker in folded:
            return city
    return 'Leipzig'


def parse_card(card):
    link = card.select_one('h4 a[href], a.link-detail[href]')
    title = clean_text(card.select_one('h4'))
    venue = clean_text(card.select_one('.news-item-location'))
    event_date, event_time = parse_datetime(card.select_one('.news-item-date'))
    if not link or not title or not venue or not event_date:
        return None
    return {
        'title': title.replace('\n', ' '),
        'date': event_date,
        'url': urljoin(SOURCE_URL, link.get('href')),
        'time_from': event_time,
        'venue': venue,
        'city': city_from_venue(venue),
        'country_code': 'DE',
        'description': clean_text(card.select_one('.news-cutted-text')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_calendar(soup):
    return [
        record for card in soup.select('.news-list-item')
        if (record := parse_card(card))
    ]


def enrich_record(session, record):
    soup = get_soup(session, record['url'])
    article = soup.select_one('.news-single .news-list-item, .news-single .article')
    if not article:
        return record
    detail_venue = clean_text(article.select_one('.news-item-location'))
    if detail_venue:
        record['venue'] = detail_venue
        record['city'] = city_from_venue(detail_venue)
    parts = []
    for node in article.select('.news-cutted-text, .news-text-wrap, .teaser-text'):
        value = clean_text(node)
        if value and value not in parts:
            parts.append(value)
    if parts:
        record['description'] = '\n\n'.join(parts)
    return record


def get_concerts():
    session = make_session()
    today = date.today()
    pages = []

    # Published future events are normally added within the next academic year.
    for offset in range(0, 19):
        year, month = add_month(today.year, today.month, offset)
        pages.append(calendar_url(CURRENT_URL, year, month))

    # Walk the complete usable archive, stopping only after a full empty year.
    empty_months = 0
    for offset in range(0, -241, -1):
        year, month = add_month(today.year, today.month, offset)
        url = calendar_url(ARCHIVE_URL, year, month)
        try:
            records = parse_calendar(get_soup(session, url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape HMT Leipzig archive month',
                event='crawler_page_failed', level='warning', url=url,
                error_type=type(error).__name__, error_message=str(error),
            )
            continue
        pages_records = records
        if pages_records:
            empty_months = 0
        else:
            empty_months += 1
        pages.append((url, pages_records))
        if empty_months >= 12:
            break

    records = []
    for page in pages:
        if isinstance(page, tuple):
            records.extend(page[1])
            continue
        try:
            records.extend(parse_calendar(get_soup(session, page)))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape HMT Leipzig calendar month',
                event='crawler_page_failed', level='warning', url=page,
                error_type=type(error).__name__, error_message=str(error),
            )

    unique = {
        (item['url'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    enriched = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(enrich_record, session, item): item for item in unique.values()}
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape HMT Leipzig event detail',
                    event='crawler_item_failed', level='warning', url=record['url'],
                    error_type=type(error).__name__, error_message=str(error),
                )
                enriched.append(record)
    return sorted(enriched, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url'],
    ))


class HmtLeipzigDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hmt_leipzig_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    HmtLeipzigDeCrawler().run()


if __name__ == '__main__':
    main()
