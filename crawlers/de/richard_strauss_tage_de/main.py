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


SOURCE_URL = 'https://www.richard-strauss-tage.de/'
SOURCE = 'Richard Strauss Tage'
PAGES_API = urljoin(SOURCE_URL, 'wp-json/wp/v2/pages')
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
DATE_TIME_RE = re.compile(
    r'(?<!\d)(\d{1,2})\.(\d{1,2})\.(20\d{2})'
    r'(?:\s+(\d{1,2}):([0-5]\d)\s*(?:Uhr)?)?',
    re.I,
)
POSTCODE_CITY_RE = re.compile(r'\b\d{5}\s+([^,\n]+)')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '').replace('\xad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
    )))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_urls(session):
    response = session.get(
        PAGES_API,
        params={'per_page': 100, '_fields': 'link,slug'},
        timeout=45,
    )
    response.raise_for_status()
    archives = [
        page['link'] for page in response.json()
        if re.fullmatch(r'programm20\d{2}', page.get('slug', ''))
    ]
    return [SOURCE_URL, *sorted(set(archives), reverse=True)]


def listing_events(soup):
    events = {}
    for article in soup.select('article.event'):
        link = article.select_one('header.main-title a[href*="/event/"]')
        if not link:
            continue
        url = urljoin(SOURCE_URL, link['href'])
        venue = clean_text(article.select_one('.evlocat')).replace('\n', ', ')
        title = clean_text(article.select_one('h3.title')) or clean_text(link)
        if url not in events or (venue and not events[url]['venue']):
            events[url] = {'url': url, 'venue': venue, 'listing_title': title}
    return events


def parse_detail(soup, item):
    title = clean_text(soup.select_one('h1.event-title')) or item['listing_title']
    date_text = clean_text(soup.select_one('.event > .col-xs-12 .evdate .like-h5'))
    match = DATE_TIME_RE.search(date_text)
    if not title or not match:
        return None
    try:
        event_date = date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None
    time_from = None
    if match.group(4):
        hour = int(match.group(4))
        if hour < 24:
            time_from = f'{hour:02d}:{match.group(5)}'

    address = clean_text(soup.select_one('.event > .col-xs-12 .evdate .evlocat'))
    city_match = POSTCODE_CITY_RE.search(address)
    city = city_match.group(1).strip() if city_match else 'Garmisch-Partenkirchen'
    venue = item['venue']
    if not venue:
        return None

    description_parts = []
    for selector in ('#event-programme', '#event-description'):
        value = clean_text(soup.select_one(selector))
        if value:
            description_parts.append(value)
    description = '\n\n'.join(description_parts) or None
    return {
        'title': title,
        'date': event_date,
        'url': item['url'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class RichardStraussTageDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='richard_strauss_tage_de',
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
        session = make_session()
        events = {}
        for url in calendar_urls(session):
            try:
                for event_url, item in listing_events(get_soup(session, url)).items():
                    events.setdefault(event_url, item)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Richard Strauss Tage calendar',
                    event='crawler_page_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(get_soup, session, item['url']): item
                for item in events.values()
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    record = parse_detail(future.result(), item)
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped Richard Strauss Tage event with incomplete data',
                            event='crawler_item_skipped', level='warning', url=item['url'],
                            error_type='IncompleteEventData',
                            error_message='Missing valid title, date, or venue',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Richard Strauss Tage event',
                        event='crawler_item_failed', level='warning', url=item['url'],
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url'],
        ))


def main():
    RichardStraussTageDeCrawler().run()


if __name__ == '__main__':
    main()
