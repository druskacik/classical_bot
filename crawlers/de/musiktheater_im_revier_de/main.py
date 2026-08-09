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


SOURCE_URL = 'https://mirgelsenkirchen.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalender-tickets')
SOURCE = 'Musiktheater im Revier'

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
    text = text.replace('\xad', '').replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=Retry(
            total=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_event(event):
    title_node = event.select_one('a.m-event__content__title[href]')
    moment_node = event.select_one('time[datetime]')
    venue = clean_text(event.select_one('.m-event__content__info__location'))
    if not title_node or not moment_node or not venue:
        return None

    moment = moment_node.get('datetime', '').strip()
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2}))?', moment)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    title = clean_text(title_node)
    url = urljoin(CALENDAR_URL, title_node.get('href', ''))
    if not title or not url:
        return None

    subtitle = clean_text(event.select_one('.m-event__content__details__subtitle'))
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}' if match.group(2) else None,
        'venue': venue,
        # Every venue in this institution's calendar is a named MiR venue in
        # Gelsenkirchen. The calendar does not include touring performances.
        'city': 'Gelsenkirchen',
        'country_code': 'DE',
        'description': subtitle or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
        '_production_uid': event.get('data-production-uid') or url,
    }


def parse_calendar(soup):
    return [record for event in soup.select('article.m-event') if (record := parse_event(event))]


def detail_description(session, url):
    soup = get_soup(session, url)
    # This block contains the long synopsis and, where published, the full
    # programme. It deliberately excludes ticketing, cast and address blocks.
    return clean_text(soup.select_one('.o-event-detail__main__content__description .m-clamp__content')) or None


def get_concerts():
    session = make_session()
    records = parse_calendar(get_soup(session, CALENDAR_URL))

    representative = {}
    for record in records:
        representative.setdefault(record['_production_uid'], record['url'])

    descriptions = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, url): (production_uid, url)
            for production_uid, url in representative.items()
        }
        for future in as_completed(futures):
            production_uid, url = futures[future]
            try:
                descriptions[production_uid] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Musiktheater im Revier production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record.pop('_production_uid'))
        if detail:
            record['description'] = '\n\n'.join(
                part for part in (record['description'], detail) if part
            )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue'], item['url']
    ))


class MusiktheaterImRevierDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musiktheater_im_revier_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        # MiR presents opera, concerts, dance, youth events, discussions and
        # other theatre formats, so records require downstream classification.
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
    MusiktheaterImRevierDeCrawler().run()


if __name__ == '__main__':
    main()
