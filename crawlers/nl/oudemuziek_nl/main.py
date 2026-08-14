import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://oudemuziek.nl/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda/')
AGENDA_API = urljoin(SOURCE_URL, 'api/collections/all/agendaitems?lang=nl-NL')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Festival Oude Muziek Utrecht'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}
MONTHS = {
    'jan': 1, 'feb': 2, 'mrt': 3, 'apr': 4, 'mei': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}
EVENT_PATH = re.compile(r'^/agenda/alle-concerten-[^/]+/[^/]+/[^/]+/?$')


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def fetch(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def api_event_urls(session):
    payload = {
        'Limit': 1000,
        'Offset': 0,
        'OrderItems': [{'Key': 'startDate', 'Direction': 'asc'}],
        'FilterItems': [],
        'Text': '',
    }
    response = session.post(AGENDA_API, json=payload, timeout=45)
    response.raise_for_status()
    data = response.json()
    return {
        urljoin(SOURCE_URL, item['detailUrl'])
        for item in data.get('items', [])
        if item.get('detailUrl')
    }


def sitemap_event_urls(session):
    soup = BeautifulSoup(fetch(session, SITEMAP_URL).content, 'xml')
    urls = set()
    for node in soup.select('url > loc'):
        url = clean_text(node)
        if EVENT_PATH.fullmatch(urlparse(url).path):
            urls.add(url)
    return urls


def discover_urls(session):
    urls = sitemap_event_urls(session)
    try:
        urls.update(api_event_urls(session))
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Agenda API discovery failed; continuing with sitemap events',
            event='crawler_discovery_failed',
            level='warning',
            url=AGENDA_API,
            error_type=type(error).__name__,
            error_message=str(error),
        )
    return sorted(urls)


def parse_date(value):
    match = re.search(r'(\d{1,2})\s+([a-z]{3})\.?\s+(\d{4})', value.lower())
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(soup):
    location = clean_text(soup.select_one('.order-banner-infolist dd'))
    if ',' not in location:
        return None, None
    city, venue = (part.strip() for part in location.split(',', 1))
    return (venue or None), (city or None)


def detail_description(soup):
    article = soup.select_one('article.content')
    if not article:
        return None
    for heading in article.select('h2:first-child'):
        heading.decompose()
    text = clean_text(article, separator='\n')
    return text or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.title'))
    subtitle = clean_text(soup.select_one('.headervisual-caption-box'))
    if subtitle and subtitle.lower() not in title.lower():
        title = f'{title} – {subtitle}'

    date_text = clean_text(soup.select_one('.order-banner-datetime .date'))
    event_date = parse_date(date_text)
    time_text = clean_text(soup.select_one('.order-banner-datetime .time'))
    time_match = re.search(r'(?<!\d)([01]?\d|2[0-3]):[0-5]\d', time_text)
    time_from = time_match.group(0).zfill(5) if time_match else None
    venue, city = parse_location(soup)

    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'NL',
        'description': detail_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(session, url):
    return parse_detail(fetch(session, url).text, url)


class OudeMuziekNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oudemuziek_nl',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = discover_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(scrape_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape agenda detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url'],
            ),
        )


def main():
    OudeMuziekNlCrawler().run()


if __name__ == '__main__':
    main()
