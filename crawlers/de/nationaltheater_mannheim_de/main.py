import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nationaltheater-mannheim.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'spielplan/')
SCHEDULE_API = urljoin(SOURCE_URL, 'callbacks/getschedule.json')
SOURCE = 'Nationaltheater Mannheim'
CITY = 'Mannheim'

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
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
    )))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def schedule_fragments(session):
    soup = get_soup(session, SCHEDULE_URL)
    content = soup.select_one('.js-schedule-content')
    if not content:
        return []

    fragments = [content]
    dates_to = content.get('data-dates-to')
    final_date = content.get('data-load-forward-until')
    while dates_to and final_date and dates_to < final_date:
        response = session.get(
            SCHEDULE_API,
            params={'loadForwardFrom': dates_to},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('ResultCode') != 'Ok' or not payload.get('Schedule'):
            break
        next_date = payload.get('DatesTo')
        if not next_date or next_date <= dates_to:
            break
        fragments.append(BeautifulSoup(payload['Schedule'], 'html.parser'))
        dates_to = next_date
    return fragments


def listing_description(card):
    parts = []
    for selector in ('.headline__subtitle', '.performance__additionalinfo'):
        value = clean_text(card.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def parse_performance(card):
    link = card.select_one('.performance__link[href]')
    title = clean_text(card.select_one('[itemprop="name"]'))
    start = card.select_one('meta[itemprop="startDate"][content]')
    venue = clean_text(card.select_one('.performance__location'))
    if not link or not title or not start or not venue:
        return None
    try:
        moment = datetime.fromisoformat(start['content'])
    except (TypeError, ValueError):
        return None
    return {
        'title': title,
        'date': moment.date().isoformat(),
        'url': urljoin(SCHEDULE_URL, link['href']),
        'time_from': moment.strftime('%H:%M'),
        'venue': venue,
        # The published calendar is for NTM's Mannheim stages. Its venue names
        # are linked to the site's Mannheim venue directory.
        'city': CITY,
        'country_code': 'DE',
        'description': listing_description(card),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(soup):
    parts = []
    for selector in (
        '.productionhead__text',
        'main .richtext',
        '.productioncastcrew',
    ):
        for node in soup.select(selector):
            value = clean_text(node)
            if value and value not in parts:
                parts.append(value)
    return '\n\n'.join(parts) or None


def enrich_record(session, record):
    description = detail_description(get_soup(session, record['url']))
    if description:
        record['description'] = description
    return record


def get_concerts():
    session = make_session()
    records = []
    for fragment in schedule_fragments(session):
        records.extend(
            record
            for card in fragment.select('.performance')
            if (record := parse_performance(card))
        )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    records = list(unique.values())

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(enrich_record, session, record): record
            for record in records
        }
        enriched = []
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched.append(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Nationaltheater Mannheim event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                enriched.append(record)

    return sorted(enriched, key=lambda item: (
        item['date'], item['time_from'] or '', item['venue'], item['title'], item['url']
    ))


class NationaltheaterMannheimDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nationaltheater_mannheim_de',
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
    NationaltheaterMannheimDeCrawler().run()


if __name__ == '__main__':
    main()
