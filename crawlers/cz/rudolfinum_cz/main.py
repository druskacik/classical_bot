import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


BASE_URL = 'https://www.rudolfinum.cz'
SOURCE_URL = f'{BASE_URL}/'
SOURCE = 'Rudolfinum'

# The main calendar contains concerts.  The education calendar also contains
# family and educational performances, but mixes them with workshops.  The
# combined feed is therefore sent through potential-event classification.
FEEDS = [
    f'{BASE_URL}/program/',
    f'{BASE_URL}/program-pro-deti/',
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}


def clean_text(value):
    if not value:
        return ''
    value = value.replace('\xa0', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def get_soup(session, url):
    response = session.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def page_url(feed_url, page):
    if page == 1:
        return feed_url
    return f'{feed_url}?strana={page}'


def last_page(soup):
    pages = []
    for link in soup.select('.paging a[href]'):
        text = clean_text(link.get_text(' ', strip=True))
        if text.isdigit():
            pages.append(int(text))
    return max(pages, default=1)


def parse_datetime(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_card(card):
    title_link = card.select_one('.events-catalog__title a[href]')
    date_element = card.select_one('[itemprop="startDate"][content]')
    venue_element = card.select_one('[itemprop="location"] [itemprop="name"]')
    city_element = card.select_one('[itemprop="addressLocality"]')
    if not all([title_link, date_element, venue_element, city_element]):
        return None

    title = clean_text(title_link.get_text(' ', strip=True))
    date, time_from = parse_datetime(date_element.get('content'))
    venue = clean_text(venue_element.get_text(' ', strip=True))
    city = clean_text(city_element.get_text(' ', strip=True))
    if not all([title, date, venue, city]):
        return None

    return {
        'title': title,
        'date': date,
        'url': urljoin(BASE_URL, title_link['href']),
        'time_from': time_from,
        'time_to': None,
        'venue': venue,
        'city': city,
        'description': None,
    }


def parse_listing(soup):
    records = []
    for card in soup.select('.events-catalog__card'):
        record = parse_card(card)
        if record:
            records.append(record)
    return records


def scrape_feed(session, feed_url):
    first_soup = get_soup(session, feed_url)
    records = parse_listing(first_soup)
    page_count = last_page(first_soup)

    for page in range(2, page_count + 1):
        records.extend(parse_listing(get_soup(session, page_url(feed_url, page))))

    return records


def extract_description(url):
    session = requests.Session()
    soup = get_soup(session, url)
    detail = soup.select_one('.event-detail__info')
    if not detail:
        return None

    for element in detail.select('script, style, picture, img, .event-detail__tags'):
        element.decompose()
    description = clean_text(detail.get_text('\n', strip=True))
    return description or None


def get_descriptions(urls):
    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(extract_description, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                descriptions[url] = None
                log_message(
                    'Failed to scrape Rudolfinum event detail',
                    event='crawler_item_failed',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return descriptions


class RudolfinumCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rudolfinum_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'time_to',
            'venue',
            'city',
            'description',
        ],
        dedupe_subset=['title', 'date', 'url', 'time_from', 'venue'],
        front_fields=[
            ('source_url', SOURCE_URL),
            ('source', SOURCE),
        ],
    )

    def scrape(self):
        session = requests.Session()
        records = []
        for feed_url in FEEDS:
            records.extend(scrape_feed(session, feed_url))

        descriptions = get_descriptions({record['url'] for record in records})
        for record in records:
            record['description'] = descriptions.get(record['url'])
        return records


def main():
    RudolfinumCrawler().run()


if __name__ == '__main__':
    main()
