import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nno.nu/'
SITEMAP_URL = 'https://nno.nu/sitemap_index.xml'
SOURCE = 'Noord Nederlands Orkest'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}
MONTHS = {
    'januari': 1,
    'februari': 2,
    'maart': 3,
    'april': 4,
    'mei': 5,
    'juni': 6,
    'juli': 7,
    'augustus': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'december': 12,
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, parser)


def concert_links(session):
    soup = get_soup(session, SITEMAP_URL, parser='xml')
    links = set()
    for location in soup.find_all('loc'):
        url = clean_text(location.get_text())
        if urlparse(url).path.startswith('/concert/'):
            links.add(url)
    return sorted(links)


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([a-z]+)\s+(\d{4})', clean_text(value).lower())
    if not match:
        return None
    day, month_name, year = match.groups()
    month = MONTHS.get(month_name)
    if not month:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def parse_location(value):
    parts = [part.strip() for part in clean_text(value).rsplit(',', 1)]
    if len(parts) != 2 or not all(parts):
        return None, None
    return parts[0], parts[1]


def detail_records(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('.section--hero-concert .section__title'))
    description = clean_text(soup.select_one('.block--single-concert')) or None
    if not title:
        return []

    records = []
    for row in soup.select('.block--data tr'):
        event_date = parse_date(row.select_one('.date'))
        venue, city = parse_location(row.select_one('.location'))
        time_from = clean_text(row.select_one('.time')) or None
        if time_from and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
            time_from = None
        if not event_date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'NL',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = concert_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(detail_records, session, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'],
            record['venue'], record['url'],
        ),
    )


class NnoNuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nno_nu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    NnoNuCrawler().run()


if __name__ == '__main__':
    main()
