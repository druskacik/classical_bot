from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://konzertverein.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap_index.xml'
SOURCE = 'Società dei Concerti di Bolzano'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def sitemap_urls(session):
    index = get_soup(session, SITEMAP_URL, 'xml')
    sitemap_links = [
        clean_text(node)
        for node in index.find_all('loc')
        if re.search(r'/concerti-sitemap\d*\.xml$', clean_text(node))
    ]

    urls = []
    for sitemap_url in sitemap_links:
        sitemap = get_soup(session, sitemap_url, 'xml')
        for node in sitemap.find_all('loc'):
            url = clean_text(node)
            path = urlparse(url).path
            if path.startswith('/concerti/') and path != '/concerti/' and url not in urls:
                urls.append(url)
    return urls


def item_text(event, itemprop):
    node = event.select_one(f'[itemprop="{itemprop}"]')
    if node is None:
        return ''
    return clean_text(node.get('content')) if node.has_attr('content') else clean_text(node)


def parse_event(soup, url):
    event = soup.select_one('[itemscope][itemtype$="/MusicEvent"]')
    if event is None:
        return None

    title = item_text(event, 'name')
    start_value = item_text(event, 'startDate')
    try:
        start = datetime.fromisoformat(start_value)
    except (TypeError, ValueError):
        return None

    location = event.select_one('[itemprop="location"]')
    venue_node = location.select_one('meta[itemprop="name"]') if location else None
    venue = clean_text(venue_node.get('content')) if venue_node else ''

    # These archived pages describe online broadcasts or recordings, not an
    # attendance-based public concert occurrence.
    virtual_text = f'{title} {venue}'.casefold()
    if any(term in virtual_text for term in ('streaming', 'youtube', 'online')):
        return None

    if not title or not venue:
        return None

    description_parts = []
    for itemprop in ('performer', 'workPerformed', 'description'):
        value = item_text(event, itemprop)
        if value and value not in description_parts:
            description_parts.append(value)

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': 'Bolzano',
        'country_code': 'IT',
        'description': clean_text('\n\n'.join(description_parts)) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return parse_event(get_soup(session, url), url)


class KonzertvereinOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='konzertverein_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            urls = sitemap_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Konzertverein concert sitemaps',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, TypeError, ValueError) as error:
                    log_message(
                        'Failed to parse Konzertverein event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    KonzertvereinOrgCrawler().run()


if __name__ == '__main__':
    main()
