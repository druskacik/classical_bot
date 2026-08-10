from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.femas.es/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programacion')
SOURCE = 'Festival de Música Antigua de Sevilla (FeMÀS)'
CITY = 'Sevilla'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
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
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response


def programme_urls(session):
    soup = BeautifulSoup(get_response(session, PROGRAM_URL).text, 'html.parser')
    urls = []
    for link in soup.select('#content-core article.newsItem a.blogItem__link[href]'):
        url = urljoin(PROGRAM_URL, link.get('href', '').strip())
        if url.startswith(f'{PROGRAM_URL}/'):
            urls.append(url)
    return list(dict.fromkeys(urls))


def description_from_page(soup):
    event = soup.select_one('#content-core > .event')
    if event is None:
        return None

    # The first visible row contains the artistic credits, programme, notes,
    # and biographies. The following row is only a repeated event summary.
    content = event.select_one(':scope > .row.mb-4')
    if content is None:
        return None
    for element in content.select(
        'script, style, img, picture, .socialShare, .documentActions'
    ):
        element.decompose()
    return clean_text(content) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1.documentFirstHeading'))
    start_node = soup.select_one('#content-core li.dtstart[itemprop="startDate"]')
    venue = clean_text(soup.select_one('.eventDetails .event-location .location'))
    if not venue:
        location = soup.select_one('.eventDetails .event-location')
        venue = re.sub(r'^Dónde\s*', '', clean_text(location), flags=re.I).strip()

    start_value = clean_text(start_node)
    try:
        start = datetime.fromisoformat(start_value)
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue:
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'ES',
        'description': description_from_page(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(session, url):
    return parse_event(get_response(session, url).text, url)


class FemasEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='femas_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = programme_urls(session)
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_event, session, url): url for url in urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape FeMÀS concert detail',
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
                        'Skipped incomplete FeMÀS concert',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, or URL is missing',
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    FemasEsCrawler().run()


if __name__ == '__main__':
    main()
