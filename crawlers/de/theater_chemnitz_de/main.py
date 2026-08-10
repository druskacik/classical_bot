import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-chemnitz.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'spielplan')
SOURCE = 'Theater Chemnitz'

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


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=8,
        pool_maxsize=8,
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


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def calendar_pages(session):
    first_soup = get_soup(session, CALENDAR_URL)
    urls = {CALENDAR_URL}
    for link in first_soup.select('a[href*="tx_ccrepertoire_list"]'):
        href = link.get('href', '')
        if ('%5Btype%5D=alle' in href or '[type]=alle' in href) and (
                '%5Bmonth%5D=' in href or '[month]=' in href):
            urls.add(urljoin(CALENDAR_URL, href))
    return first_soup, sorted(urls)


def parse_card(card):
    link = card.select_one('a[href*="/spielplan/detailseite/"]')
    title = clean_text(card.select_one('h2'))
    date_text = clean_text(card.select_one('.cc_date'))
    time_text = clean_text(card.select_one('.cc_time'))
    venue = clean_text(card.select_one('.cc_loc'))
    if not link or not title or not date_text or not venue:
        return None

    try:
        date = datetime.strptime(date_text, '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None

    time_match = re.search(r'\b(\d{1,2})[.:](\d{2})\b', time_text)
    time_from = None
    if time_match:
        hour, minute = map(int, time_match.groups())
        if hour < 24 and minute < 60:
            time_from = f'{hour:02d}:{minute:02d}'

    subtitle = clean_text(card.select_one('h3'))
    event_type = clean_text(card.select_one('.cc_type'))
    summary = '\n'.join(value for value in (subtitle, event_type) if value) or None
    return {
        'title': title,
        'date': date,
        'url': canonical_url(urljoin(CALENDAR_URL, link['href'])),
        'time_from': time_from,
        'venue': venue,
        # The institution's unqualified calendar venues are all in Chemnitz.
        # Touring dates on this site identify the external city in the venue.
        'city': venue_city(venue),
        'country_code': 'DE',
        'description': summary,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def venue_city(venue):
    tour_cities = {
        'Dresden': 'Dresden',
        'Zwickau': 'Zwickau',
        'Leipzig': 'Leipzig',
        'Gera': 'Gera',
        'Plauen': 'Plauen',
        'Freiberg': 'Freiberg',
    }
    folded = venue.casefold()
    for marker, city in tour_cities.items():
        if marker.casefold() in folded:
            return city
    return 'Chemnitz'


def parse_page(soup):
    records = []
    for card in soup.select('.cc_event'):
        record = parse_card(card)
        if record and record['city']:
            records.append(record)
    return records


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    header = soup.select_one('.cc_page_hl h2')
    body = soup.select_one('.cc_sp_description')
    for node in (header, body):
        value = clean_text(node)
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = make_session()
    first_soup, page_urls = calendar_pages(session)
    records = parse_page(first_soup)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, session, url): url
            for url in page_urls if url != CALENDAR_URL
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_page(future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Theater Chemnitz calendar page',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    records = list(unique.values())

    production_urls = {re.sub(r'/\d+$', '', record['url']) for record in records}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, url): url
            for url in production_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Theater Chemnitz event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(re.sub(r'/\d+$', '', record['url']))
        if detail:
            record['description'] = detail

    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url']
    ))


class TheaterChemnitzDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_chemnitz_de',
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
    TheaterChemnitzDeCrawler().run()


if __name__ == '__main__':
    main()
