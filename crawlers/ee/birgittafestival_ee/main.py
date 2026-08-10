import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.birgittafestival.ee/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programm')
NEWS_URL = urljoin(SOURCE_URL, 'uudised')
SOURCE = 'Birgitta Festival'
DEFAULT_VENUE = 'Pirita kloostri varemed'
DEFAULT_CITY = 'Tallinn'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'et-EE,et;q=0.9,en;q=0.7',
}

MONTHS = {
    'jaanuar': 1,
    'veebruar': 2,
    'märts': 3,
    'aprill': 4,
    'mai': 5,
    'juuni': 6,
    'juuli': 7,
    'august': 8,
    'september': 9,
    'oktoober': 10,
    'november': 11,
    'detsember': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=60)
    try:
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Birgitta Festival page',
            event='crawler_fetch_failed',
            level='error',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise
    return BeautifulSoup(response.text, 'html.parser')


def programme_year(news_soup):
    main = news_soup.find('main')
    if not main:
        return None
    # The news archive starts with the year of the programme currently retained
    # by the site, followed by older archive sections.
    match = re.search(r'\b(20\d{2})\b', clean_text(main.get_text(' ', strip=True)))
    return int(match.group(1)) if match else None


def item_parts(item):
    blocks = [
        clean_text(node.get_text(' ', strip=True))
        for node in item.select('.wixui-rich-text')
        if clean_text(node.get_text(' ', strip=True))
    ]
    if len(blocks) < 2:
        return None
    metadata, title = blocks[0], blocks[-1]
    match = re.search(
        r'(?P<days>\d{1,2}\.(?:\s*(?:ja|,|-)\s*\d{1,2}\.)*)\s*'
        r'(?P<month>[A-Za-zÀ-ſ]+)\s*'
        r'(?P<hour>[0-2]?\d)[.:](?P<minute>[0-5]\d)',
        metadata,
        flags=re.IGNORECASE,
    )
    link = item.find('a', href=True, string=lambda value: value and value.strip() == 'INFO')
    if not match or not title or not link:
        return None
    month = MONTHS.get(match.group('month').lower())
    days = [int(value) for value in re.findall(r'\d{1,2}', match.group('days'))]
    if not month or not days:
        return None
    venue = 'Eesti Metodisti kirik' if 'Eesti Metodisti kirik' in metadata else DEFAULT_VENUE
    return title, days, month, f"{int(match.group('hour')):02d}:{match.group('minute')}", venue, urljoin(PROGRAM_URL, link['href'])


def detail_description(session, url):
    soup = fetch_soup(session, url)
    main = soup.find('main')
    return clean_text(main.get_text('\n', strip=True)) or None if main else None


def parse_program(program_soup, year, descriptions):
    records = []
    for item in program_soup.select('.wixui-repeater__item'):
        parts = item_parts(item)
        if not parts:
            continue
        title, days, month, time_from, venue, url = parts
        for day in days:
            try:
                event_date = date(year, month, day).isoformat()
            except (TypeError, ValueError):
                continue
            records.append(
                {
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': DEFAULT_CITY,
                    'country_code': 'EE',
                    'description': descriptions.get(url),
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                }
            )
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    program_soup = fetch_soup(session, PROGRAM_URL)
    year = programme_year(fetch_soup(session, NEWS_URL))
    if not year:
        log_message(
            'Could not determine Birgitta Festival programme year',
            event='crawler_parse_failed',
            level='error',
            url=NEWS_URL,
        )
        return []

    detail_urls = set()
    for item in program_soup.select('.wixui-repeater__item'):
        parts = item_parts(item)
        if parts:
            detail_urls.add(parts[-1])

    descriptions = {}
    for url in detail_urls:
        try:
            descriptions[url] = detail_description(session, url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Birgitta Festival event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(
        parse_program(program_soup, year, descriptions),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class BirgittafestivalEeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='birgittafestival_ee',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='EE',
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
    BirgittafestivalEeCrawler().run()


if __name__ == '__main__':
    main()
