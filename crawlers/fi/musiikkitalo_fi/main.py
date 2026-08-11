import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musiikkitalo.fi/'
SITEMAP_URL = f'{SOURCE_URL}sitemap_index.xml'
SOURCE = 'Musiikkitalo'
CITY = 'Helsinki'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def sitemap_locations(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return [clean_text(node) for node in soup.select('loc') if clean_text(node)]


def event_urls():
    sitemap_urls = [
        url for url in sitemap_locations(SITEMAP_URL)
        if re.search(r'/event-sitemap\d*\.xml$', url)
    ]
    urls = set()
    for sitemap_url in sitemap_urls:
        try:
            urls.update(
                url for url in sitemap_locations(sitemap_url)
                if '/konsertit-ja-tapahtumat/' in url
            )
        except requests.RequestException as error:
            log_message(
                'Failed to read Musiikkitalo event sitemap',
                event='crawler_item_failed',
                level='warning',
                url=sitemap_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(urls)


def parse_date_time(soup):
    value = clean_text(soup.select_one('.block-single-event .date-and-time'))
    date_match = re.search(r'\b(\d{1,2}\.\d{1,2}\.\d{4})\b', value)
    if not date_match:
        return None, None
    try:
        event_date = datetime.strptime(date_match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None, None

    time_value = value[date_match.end():]
    time_match = re.search(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\b', time_value)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, time_from


def parse_event(soup, url):
    container = soup.select_one('.block-single-event')
    if not container:
        return None

    title = clean_text(container.select_one('h1'))
    event_date, time_from = parse_date_time(soup)
    venue = clean_text(container.select_one('.event-info-content .location'))
    description = clean_text(container.select_one('.content-wrapper .content')) or None
    if not all((title, event_date, url, venue)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'FI',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events():
    urls = event_urls()
    records = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(get_soup, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result(), url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Musiikkitalo event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class MusiikkitaloFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musiikkitalo_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_events()


def main():
    MusiikkitaloFiCrawler().run()


if __name__ == '__main__':
    main()
