import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.elbphilharmonie.de/de/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programm/')
SOURCE = 'Elbphilharmonie Hamburg'
CITY = 'Hamburg'

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


def fetch(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def available_range(session):
    soup = fetch(session, PROGRAM_URL)
    calendar = soup.select_one('.calendar[data-first_event_date][data-last_event_date]')
    if not calendar:
        return date.today(), date.today() + timedelta(days=365)
    first = datetime.fromisoformat(calendar['data-first_event_date']).date()
    last = datetime.fromisoformat(calendar['data-last_event_date']).date()
    return first, last


def listing_urls(session):
    first, last = available_range(session)
    # A dated programme page shows roughly the following two weeks. Weekly
    # sampling therefore covers the archive and future catalogue with overlap.
    days = []
    current = first
    while current <= last:
        days.append(current)
        current += timedelta(days=7)
    if not days or days[-1] != last:
        days.append(last)

    urls = set()
    for day in days:
        url = urljoin(PROGRAM_URL, day.strftime('%d-%m-%Y') + '/')
        try:
            soup = fetch(session, url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape programme page', event='crawler_page_failed',
                level='warning', url=url, error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for link in soup.select('#event-list li.event-item .event-title a[href]'):
            href = link.get('href', '')
            if re.search(r'/de/programm/(?!ticket/)[^/]+/\d+/?$', href):
                urls.add(urljoin(SOURCE_URL, href))
    return sorted(urls)


def section_after_heading(soup, heading):
    node = next(
        (item for item in soup.select('h2, h3') if clean_text(item).lower() == heading.lower()),
        None,
    )
    if not node:
        return ''
    if heading.lower() == 'beschreibung':
        container = node.find_parent(class_='event-detail-content')
        column = node.find_parent(class_=lambda value: value and 'cell' in value.split())
        return clean_text(column or container)
    wrapper = node.find_next_sibling()
    return clean_text(wrapper)


def parse_detail(soup, url):
    header = soup.select_one('.event-detail-head')
    time_node = header.select_one('time[datetime]') if header else None
    title = clean_text(header.select_one('h1.event-title')) if header else ''
    place = clean_text(header.select_one('.place')) if header else ''
    place = re.sub(r'\s+', ' ', place).strip()
    if not title or not time_node or not place:
        return None
    try:
        starts_at = datetime.fromisoformat(time_node['datetime'])
    except (KeyError, ValueError):
        return None

    subtitle = clean_text(header.select_one('.event-subtitle'))
    description = section_after_heading(soup, 'Beschreibung')
    programme = section_after_heading(soup, 'Programm')
    parts = [part for part in (subtitle, description, programme) if part]
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': place,
        'city': CITY,
        'country_code': 'DE',
        'description': '\n\n'.join(parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return parse_detail(fetch(session, url), url)


class ElbphilharmonieDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='elbphilharmonie_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        urls = listing_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(scrape_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape concert detail', event='crawler_item_failed',
                        level='warning', url=url, error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


def main():
    ElbphilharmonieDeCrawler().run()


if __name__ == '__main__':
    main()
