import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cannonbeachmusicfestival.org/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Cannon Beach Music Festival'
VENUE = 'Cannon Beach Community Church'
CITY = 'Cannon Beach'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s*'
    r'([A-Z]+)\s+(\d{1,2}),\s*(\d{1,2}:\d{2}\s*[AP]M)',
    re.IGNORECASE,
)
PAGE_RE = re.compile(r'^/concerts(?:\d{4})?/?$')


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def concert_pages(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    pages = {
        clean_text(node.get_text())
        for node in soup.select('url > loc')
        if PAGE_RE.fullmatch(urlparse(clean_text(node.get_text())).path)
    }
    pages.add(urljoin(SOURCE_URL, 'concerts'))
    return sorted(pages)


def page_year(soup, url):
    match = re.search(r'/concerts(\d{4})/?$', urlparse(url).path)
    if match:
        return int(match.group(1))

    for node in soup.select('h1, h2, h3, p'):
        text = clean_text(node.get_text(' ', strip=True))
        if re.fullmatch(r'20\d{2}', text):
            return int(text)
    return None


def parse_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    year = page_year(soup, page_url)
    if year is None:
        log_message(
            'Concert page has no identifiable year',
            event='crawler_page_skipped',
            level='warning',
            url=page_url,
        )
        return []

    records = []
    for block in soup.select('.sqs-html-content'):
        heading = block.find('h1')
        date_node = block.find('p', string=lambda value: value and DATE_RE.search(value))
        if not heading or not date_node:
            continue

        match = DATE_RE.search(clean_text(date_node.get_text(' ', strip=True)))
        try:
            event_date = datetime.strptime(
                f'{match.group(1)} {match.group(2)} {year}', '%B %d %Y'
            ).date().isoformat()
            time_from = datetime.strptime(match.group(3).upper(), '%I:%M %p').strftime('%H:%M')
        except (AttributeError, ValueError):
            continue

        title = clean_text(heading.get_text(' ', strip=True))
        description_parts = []
        for node in block.find_all(['h3', 'p']):
            if node is date_node:
                continue
            text = clean_text(node.get_text(' ', strip=True))
            if text and text not in description_parts:
                description_parts.append(text)

        section = block.find_parent('section')
        ticket_link = None
        if section:
            ticket_link = section.find(
                'a', href=True, string=lambda value: value and 'ticket' in value.lower()
            )
        event_url = urljoin(page_url, ticket_link['href']) if ticket_link else page_url

        if not title:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': event_url,
            'time_from': time_from,
            'venue': VENUE,
            'city': CITY,
            'country_code': 'US',
            'description': '\n\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page_url in concert_pages(session):
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            page_records = parse_page(response.text, page_url)
            records.extend(page_records)
            log_message(
                'Concert page scraped',
                event='crawler_page_scraped',
                url=page_url,
                record_count=len(page_records),
            )
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class CannonBeachMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cannonbeachmusicfestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    CannonBeachMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
