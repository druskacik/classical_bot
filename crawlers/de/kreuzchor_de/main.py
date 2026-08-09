import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kreuzchor.de/'
EVENT_SITEMAP_URL = urljoin(SOURCE_URL, 'wp-sitemap-posts-event-1.xml')
SOURCE = 'Dresdner Kreuzchor'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    root = ElementTree.fromstring(get_response(session, EVENT_SITEMAP_URL).content)
    namespace = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    return [
        node.text.strip()
        for node in root.findall('.//sm:url/sm:loc', namespace)
        if node.text and node.text.strip()
    ]


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.type-event')
    if article is None:
        return None

    title = clean_text(article.select_one('.event-title h1, .event-title h2'))
    info = clean_text(article.select_one('.event-title .additional-info'))
    match = re.search(
        r'(\d{1,2})\.(\d{1,2})\.(20\d{2}),\s*'
        r'(\d{1,2}):(\d{2}),\s*(.+?)\s*\|\s*(.+)$',
        info,
    )
    if not title or not match:
        return None

    try:
        event_date = date(
            int(match.group(3)), int(match.group(2)), int(match.group(1))
        ).isoformat()
    except ValueError:
        return None

    city = clean_text(match.group(6)).strip(' ,-')
    venue = clean_text(match.group(7)).strip(' ,-')
    if not city or not venue:
        return None

    description_node = article.select_one('.copyblock')
    if description_node:
        for unwanted in description_node.select('a.widget_btn, script, style'):
            unwanted.decompose()
    description = clean_text(description_node) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{int(match.group(4)):02d}:{match.group(5)}',
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return parse_event(get_response(session, url).text, url)


class KreuzchorDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kreuzchor_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = event_urls(session)
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ElementTree.ParseError) as error:
                    log_message(
                        'Failed to scrape Dresdner Kreuzchor event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Dresdner Kreuzchor event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, or city is missing',
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    KreuzchorDeCrawler().run()


if __name__ == '__main__':
    main()
