import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jfcb.cz/'
LISTING_URL = urljoin(SOURCE_URL, 'koncerty')
SOURCE = 'Jihočeská filharmonie'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.7',
}

# The site omits the city from the names of its two České Budějovice halls.
HOME_VENUES = {
    'Kostel sv. Anny': 'České Budějovice',
    'Metropol – divadelní sál': 'České Budějovice',
    'Metropol - divadelní sál': 'České Budějovice',
    'Metropol – společenský sál': 'České Budějovice',
    'Metropol - společenský sál': 'České Budějovice',
}

COUNTRIES_BY_CITY = {
    'Vídeň': 'AT',
    'Vienna': 'AT',
    'Chemnitz': 'DE',
}


def clean_text(value):
    if not value:
        return ''
    value = value.replace('\xa0', ' ').replace('\u200d', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\s*[./]\s*(\d{1,2})\s*[./]\s*(\d{4})'
        r'(?:\s+(\d{1,2}:\d{2}))?',
        value or '',
    )
    if not match:
        return None, None

    day, month, year, time_from = match.groups()
    try:
        date = datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        return None, None
    return date, time_from


def location_from_venue(venue):
    if not venue:
        return None, None

    if venue in HOME_VENUES:
        return HOME_VENUES[venue], 'CZ'

    if ',' in venue:
        city = clean_text(venue.rsplit(',', 1)[1])
        city = re.sub(r'\s*\([^)]*\)\s*$', '', city).strip()
        if city:
            return city, COUNTRIES_BY_CITY.get(city, 'CZ')

    # A bare city is not a defensible venue, so touring entries such as
    # "Chemnitz" and "China" are intentionally rejected by the caller.
    return None, None


def detail_description(url):
    soup = get_soup(url)
    sections = []

    performers = soup.select_one('.section-soliste')
    if performers:
        text = clean_text(performers.get_text('\n', strip=True))
        if text:
            sections.append(text)

    body = soup.select_one('.section-detail-koncertu .rich-text')
    if body:
        text = clean_text(body.get_text('\n', strip=True))
        if text:
            sections.append(text)

    return clean_text('\n\n'.join(sections)) or None


def parse_card(card):
    title_link = card.select_one('.blog-heading-link[href]')
    date_element = card.select_one('.datum-a-cas-vypis')
    venue_element = card.select_one('.m-sto-konani')
    if not title_link or not date_element or not venue_element:
        return None

    title = clean_text(title_link.get_text(' ', strip=True))
    date, time_from = parse_datetime(date_element.get_text(' ', strip=True))
    venue = clean_text(venue_element.get_text(' ', strip=True))
    city, country_code = location_from_venue(venue)
    url = urljoin(SOURCE_URL, title_link.get('href', ''))

    if not all((title, date, url, venue, city, country_code)):
        return None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    soup = get_soup(LISTING_URL)
    concerts = []
    for card in soup.select('.blog-card'):
        concert = parse_card(card)
        if concert:
            concerts.append(concert)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(detail_description, concert['url']): concert
            for concert in concerts
        }
        for future in as_completed(futures):
            concert = futures[future]
            try:
                concert['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=concert['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return concerts


class JcfilharmonieCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jcfilharmonie_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
        upload_target='classical',
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
    JcfilharmonieCrawler().run()


if __name__ == '__main__':
    main()
