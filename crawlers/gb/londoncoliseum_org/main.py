import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://londoncoliseum.org/'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
SOURCE = 'London Coliseum'
CITY = 'London'
VENUE = 'London Coliseum'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
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
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = node.get_text(strip=True)
        if url.startswith(f'{SOURCE_URL}events/'):
            urls.append(url)

    # Some XML parsers expose the stylesheet-rendered sitemap as HTML.
    if not urls:
        urls = [
            link.get('href')
            for link in soup.select('a[href*="/events/"]')
            if link.get('href', '').startswith(f'{SOURCE_URL}events/')
        ]
    return list(dict.fromkeys(urls))


def event_occurrences(soup):
    occurrences = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            # The site's JSON-LD contains literal newlines inside some string
            # values. They are accepted by browsers but require non-strict
            # decoding in Python.
            payload = json.loads(script.string or script.get_text(), strict=False)
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        occurrences.extend(
            item for item in items
            if isinstance(item, dict) and item.get('@type') == 'Event'
        )
    return occurrences


def page_description(soup, occurrence):
    sections = [
        clean_text(node.get_text('\n', strip=True))
        for node in soup.select('.main-content__text')
    ]
    sections = [value for value in sections if value]
    if sections:
        return '\n\n'.join(dict.fromkeys(sections))
    return clean_text(occurrence.get('description')) or None


def scrape_event(session, url):
    soup = get_soup(session, url)
    occurrences = event_occurrences(soup)
    if not occurrences:
        return []

    title_node = soup.select_one('h1.page-header__heading')
    page_title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
    description = page_description(soup, occurrences[0])
    records = []
    for occurrence in occurrences:
        title = clean_text(occurrence.get('name')) or page_title
        start = occurrence.get('startDate') or ''
        location = clean_text(occurrence.get('location'))
        try:
            start_at = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            continue

        # This is a venue-specific calendar. Reject any explicitly different
        # location rather than incorrectly applying its London home venue.
        if location.casefold() != VENUE.casefold() or not title:
            continue
        records.append({
            'title': title,
            'date': start_at.date().isoformat(),
            'url': url,
            'time_from': start_at.strftime('%H:%M'),
            'venue': VENUE,
            'city': CITY,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_event, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
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
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class LondoncoliseumOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='londoncoliseum_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
    LondoncoliseumOrgCrawler().run()


if __name__ == '__main__':
    main()
