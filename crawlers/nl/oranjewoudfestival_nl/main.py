import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oranjewoudfestival.nl/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programma/')
SOURCE = 'Oranjewoud Festival'
DEFAULT_CITY = 'Oranjewoud'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'januari': 1, 'februari': 2, 'maart': 3, 'april': 4, 'mei': 5,
    'juni': 6, 'juli': 7, 'augustus': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'december': 12,
}

DATE_RE = re.compile(
    r'\b(?:ma|di|wo|do|vr|za|zo)\.\s*'
    r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}):([0-5]\d)\b')


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def programme_links(session):
    soup = get_soup(session, PROGRAMME_URL)
    links = set()
    for item in soup.select('.program__item'):
        anchor = item.select_one('a[href]')
        if not anchor:
            continue
        url = urljoin(SOURCE_URL, anchor.get('href'))
        path = urlparse(url).path.rstrip('/')
        if re.fullmatch(r'/programma/[^/]+', path):
            links.add(url)
    return sorted(links)


def parse_date_and_time(value):
    date_match = DATE_RE.search(value)
    if not date_match:
        return None, None
    day, month_name, year = date_match.groups()
    try:
        event_date = datetime(
            int(year), MONTHS[month_name.lower()], int(day)
        ).date().isoformat()
    except ValueError:
        return None, None
    time_match = TIME_RE.search(value[date_match.end():])
    if not time_match:
        return event_date, None
    return event_date, f'{int(time_match.group(1)):02d}:{time_match.group(2)}'


def event_description(soup):
    parts = []
    for node in soup.select('.event__content > .content, .event__content .collaboration'):
        text = clean_text(node, separator='\n')
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def scrape_detail(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('h1'))
    meta = soup.select_one('.event__meta')
    if not title or not meta:
        return None

    date_text = clean_text(meta.select_one('time'))
    event_date, time_from = parse_date_and_time(date_text)
    venue_link = meta.select_one('a[href*="/locaties/"]')
    venue = clean_text(venue_link)
    if not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': DEFAULT_CITY,
        'country_code': 'NL',
        'description': event_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = programme_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_detail, session, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape programme item',
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


class OranjewoudFestivalNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oranjewoudfestival_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OranjewoudFestivalNlCrawler().run()


if __name__ == '__main__':
    main()
