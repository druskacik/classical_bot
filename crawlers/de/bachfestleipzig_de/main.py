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


SOURCE_URL = 'https://www.bachfestleipzig.de/de/bachfest'
SOURCE = 'Bachfest Leipzig'
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
        pool_connections=12,
        pool_maxsize=12,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def programme_urls(session):
    soup = get_soup(session, SOURCE_URL)
    urls = {
        urljoin(SOURCE_URL, link['href']).split('?', 1)[0].split('#', 1)[0]
        for link in soup.select('a[href]')
        if re.search(r'/de/bachfest/\d{4}/konzerte/?$', link['href'])
    }
    return sorted(urls)


def listing_pages(session, programme_url):
    first = get_soup(session, programme_url)
    dates = [option.get('value') for option in first.select(
        'select[name="concert_date[]"] option[value]'
    )]
    if not dates:
        return [first]

    pages = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, session, programme_url, {'concert_date[]': date}): date
            for date in dates
        }
        for future in as_completed(futures):
            date = futures[future]
            try:
                pages.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Bachfest programme date',
                    event='crawler_page_failed',
                    level='warning',
                    url=programme_url,
                    date=date,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return pages


def city_for_venue(venue):
    lowered = venue.casefold()
    if 'rötha' in lowered:
        return 'Rötha'
    if 'störmthal' in lowered:
        return 'Großpösna'
    # The programme's unqualified venues are all in Leipzig. The two venues
    # outside the city are explicitly named above.
    return 'Leipzig'


def parse_card(card):
    link = card.select_one('.event-teaser__headline a[href]')
    title = clean_text(link)
    start = card.select_one('.date-display-start[content]')
    venue = clean_text(card.select_one('.event-location a, .event-location'))
    if not link or not title or not start or not venue:
        return None
    try:
        moment = datetime.fromisoformat(start['content'])
    except (TypeError, ValueError):
        return None

    return {
        'title': title,
        'date': moment.date().isoformat(),
        'url': urljoin(SOURCE_URL, link['href']),
        'time_from': moment.strftime('%H:%M'),
        'venue': venue,
        'city': city_for_venue(venue),
        'country_code': 'DE',
        'description': clean_text(card.select_one('.field-artists-shortname')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, record):
    soup = get_soup(session, record['url'])
    description = clean_text(soup.select_one('.event-entry .group-content'))
    if description:
        record['description'] = description
    return record


def get_concerts():
    session = make_session()
    records = []
    for programme_url in programme_urls(session):
        for soup in listing_pages(session, programme_url):
            records.extend(
                record
                for card in soup.select('article.event-teaser')
                if (record := parse_card(card))
            )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }

    enriched = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, record): record
            for record in unique.values()
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                enriched.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Bachfest concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                enriched.append(record)

    return sorted(enriched, key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['url']
    ))


class BachfestleipzigDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachfestleipzig_de',
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
    BachfestleipzigDeCrawler().run()


if __name__ == '__main__':
    main()
