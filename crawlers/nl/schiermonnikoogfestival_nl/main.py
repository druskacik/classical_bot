import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://schiermonnikoogfestival.nl/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'festivals/')
SOURCE = 'Schiermonnikoog Festival'
DEFAULT_CITY = 'Schiermonnikoog'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}
MONTHS = {
    'januari': 1, 'februari': 2, 'maart': 3, 'april': 4,
    'mei': 5, 'juni': 6, 'juli': 7, 'augustus': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'december': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def festival_pages(session):
    soup = get_soup(session, ARCHIVE_URL)
    pages = {ARCHIVE_URL}
    for anchor in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, anchor.get('href'))
        path = urlparse(url).path.rstrip('/')
        if re.fullmatch(r'/festivals/[^/]+', path):
            pages.add(url.rstrip('/') + '/')
    return sorted(pages)


def programme_pages(session):
    pages = set()
    for festival_url in festival_pages(session):
        try:
            soup = get_soup(session, festival_url)
        except requests.RequestException as error:
            log_message(
                'Failed to inspect festival page', event='crawler_item_failed',
                level='warning', url=festival_url,
                error_type=type(error).__name__, error_message=str(error),
            )
            continue
        festival_path = urlparse(festival_url).path.rstrip('/')
        for anchor in soup.select('a[href]'):
            url = urljoin(SOURCE_URL, anchor.get('href'))
            if urlparse(url).path.rstrip('/') == festival_path + '/programma':
                pages.add(url.rstrip('/') + '/')
    return sorted(pages)


def page_year(soup, url):
    heading = clean_text(soup.select_one('h1'))
    match = re.search(r'\b(20\d{2})\b', heading)
    if not match:
        match = re.search(r'\b(20\d{2})\b', url)
    return int(match.group(1)) if match else None


def event_candidates(session):
    candidates = {}
    for programme_url in programme_pages(session):
        try:
            soup = get_soup(session, programme_url)
        except requests.RequestException as error:
            log_message(
                'Failed to inspect programme page', event='crawler_item_failed',
                level='warning', url=programme_url,
                error_type=type(error).__name__, error_message=str(error),
            )
            continue
        year = page_year(soup, programme_url)
        if not year:
            continue
        for anchor in soup.select('a.card--event[href], a[href*="/evenementen/"]'):
            url = urljoin(SOURCE_URL, anchor.get('href'))
            if re.fullmatch(r'/evenementen/[^/]+/?', urlparse(url).path):
                candidates[url] = year
    return sorted(candidates.items())


def parse_date_time(value, year):
    match = re.search(
        r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')'
        r'(?:\s+(20\d{2}))?\s*,?\s*(\d{1,2}):([0-5]\d)',
        value.lower(),
    )
    if not match:
        return None
    try:
        event_year = int(match.group(3)) if match.group(3) else year
        event_date = date(event_year, MONTHS[match.group(2)], int(match.group(1)))
    except ValueError:
        return None
    return event_date.isoformat(), f'{int(match.group(4)):02d}:{match.group(5)}'


def scrape_detail(session, url, year):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('.info-block__title'))
    info_items = soup.select('.info-block__list__item')
    date_time = None
    venue = None
    for item in info_items:
        text = clean_text(item.select_one('.block__list__item__info'))
        icon = item.select_one('i')
        classes = icon.get('class', []) if icon else []
        if 'fa-clock' in classes:
            date_time = parse_date_time(text, year)
        elif 'fa-map-marker-alt' in classes:
            venue = text
    body = soup.select_one('.wrapper--content .cb__container .cb')
    description = clean_text(body) or None
    if not title or not date_time or not venue:
        return None
    return {
        'title': title,
        'date': date_time[0],
        'url': url,
        'time_from': date_time[1],
        'venue': venue,
        'city': DEFAULT_CITY,
        'country_code': 'NL',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SchiermonnikoogfestivalNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='schiermonnikoogfestival_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        candidates = event_candidates(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(scrape_detail, session, url, year): url
                for url, year in candidates
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape event detail', event='crawler_item_failed',
                        level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'], record['title'], record['url'],
            ),
        )


def main():
    SchiermonnikoogfestivalNlCrawler().run()


if __name__ == '__main__':
    main()
