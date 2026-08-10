import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-plauen-zwickau.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'spielplan.php?ref=konzert')
SOURCE = 'Theater Plauen-Zwickau'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=90)
    response.raise_for_status()
    response.encoding = 'utf-8'
    return BeautifulSoup(response.text, 'html.parser')


def city_for_venue(venue):
    value = venue.casefold()
    if 'plauen' in value or value in {
        'vogtlandtheater',
        'kleine bühne',
        'theaterhof',
    }:
        return 'Plauen'
    if 'zwickau' in value or 'gewandhaus' in value or 'neue welt' in value:
        return 'Zwickau'
    return None


def listing_record(holder):
    category = clean_text(holder.select_one('.kategorie'))
    if 'konzert' not in category.casefold():
        return None

    date_node = holder.select_one('.complete_date')
    title_link = holder.select_one('a.title[href*="id="]')
    location_node = holder.select_one('.location.break1')
    if not all((date_node, title_link, location_node)):
        return None

    try:
        event_date = datetime.strptime(
            clean_text(date_node), '%d.%m.%Y'
        ).date().isoformat()
    except ValueError:
        return None

    title_copy = BeautifulSoup(str(title_link), 'html.parser')
    for node in title_copy.select('.subtitle'):
        node.decompose()
    title = clean_text(title_copy)

    time_node = location_node.select_one('b')
    time_text = clean_text(time_node)
    time_match = re.search(r'\b(\d{1,2}):([0-5]\d)\b', time_text)
    location_parts = [clean_text(value) for value in location_node.stripped_strings]
    venue = next(
        (
            value for value in location_parts
            if value != time_text
            and not re.search(r'\b\d{1,2}:\d{2}\b', value)
            and value.casefold() not in {
                'eintritt frei', 'wiederaufnahme', 'premiere', 'karten'
            }
        ),
        '',
    )
    city = city_for_venue(venue)
    if not all((title, venue, city)):
        return None

    url = urljoin(SOURCE_URL, title_link.get('href'))
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': (
            f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            if time_match else None
        ),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    body = soup.select_one('.detailtable > .right-cell')
    return clean_text(body) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, SCHEDULE_URL)
    records = []
    seen = set()
    for holder in soup.select('.blocked.holder'):
        record = listing_record(holder)
        if not record:
            continue
        key = (
            record['date'], record['time_from'], record['venue'],
            record['title'], record['url'],
        )
        if key not in seen:
            seen.add(key)
            records.append(record)

    descriptions = {}
    urls = sorted({record['url'] for record in records})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, url): url for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        record['description'] = descriptions.get(record['url'])
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class TheaterPlauenZwickauDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_plauen_zwickau_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['date', 'time_from', 'venue', 'title', 'url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterPlauenZwickauDeCrawler().run()


if __name__ == '__main__':
    main()
