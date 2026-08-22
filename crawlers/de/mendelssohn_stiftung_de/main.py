import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mendelssohn-stiftung.de/de/'
EVENTS_URL = urljoin(SOURCE_URL, 'konzerte')
SOURCE = 'Mendelssohn-Haus Leipzig'
CITY = 'Leipzig'

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


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    soup = get_soup(session, EVENTS_URL)
    urls = []
    for item in soup.select('li.eventlist__item'):
        link = item.select_one('a[href*="/konzerte/"]')
        if link and link.get('href'):
            urls.append(urljoin(EVENTS_URL, link['href']))
    return list(dict.fromkeys(urls))


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', clean_text(value))
    return match.group(0) if match else None


def description_from(soup):
    info = soup.select_one('.event__info')
    if not info:
        return None

    parts = []
    description = info.select_one('.event__info-description')
    if description:
        # Descriptive paragraphs are siblings of an often-empty marker element.
        container = description.parent
        value = clean_text(container)
        if value:
            parts.append(value)

    performers = clean_text(info.select_one('.event__info-solisten'))
    if performers:
        parts.append('Mitwirkende\n' + performers)

    programme = []
    for composer in info.select('.event__info-composer'):
        name = clean_text(composer)
        works_node = composer.find_next_sibling('ul', class_='event__info-works')
        works = [clean_text(item) for item in works_node.select('li')] if works_node else []
        works = [work for work in works if work]
        if name and name != '* * * *':
            programme.append(name + (('\n' + '\n'.join(works)) if works else ''))
    if programme:
        parts.append('Programm\n' + '\n\n'.join(programme))

    return '\n\n'.join(dict.fromkeys(parts)) or None


def parse_event(url, soup):
    title = clean_text(soup.select_one('.event__header-title h1'))
    date = parse_date(soup.select_one('.event__detail-date .date'))
    time_from = parse_time(soup.select_one('.event__detail-date .time'))
    venue = clean_text(soup.select_one('.event__detail-location'))
    if not title or not date or not venue:
        return None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': description_from(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(url, future.result())
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped event with incomplete required fields',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                    )
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class MendelssohnStiftungDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mendelssohn_stiftung_de',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MendelssohnStiftungDeCrawler().run()


if __name__ == '__main__':
    main()
