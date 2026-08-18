import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operafestival.fi/'
SITEMAP_URL = f'{SOURCE_URL}sitemap_index.xml'
SOURCE = 'Savonlinnan Oopperajuhlat'
CITY = 'Savonlinna'
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


def calendar_urls():
    sitemap_urls = sitemap_locations(SITEMAP_URL)
    page_sitemaps = [url for url in sitemap_urls if re.search(r'/page-sitemap\d*\.xml$', url)]
    urls = set()
    for sitemap_url in page_sitemaps:
        urls.update(
            url for url in sitemap_locations(sitemap_url)
            if re.search(r'/kalenteri(?:-\d{4})?/?$', url)
        )
    return sorted(urls)


def parse_calendar(soup, url):
    calendar = soup.select_one('.event-calendar[data-raw]')
    if not calendar:
        return []
    try:
        sections = json.loads(calendar['data-raw'])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        log_message(
            'Failed to parse Opera Festival calendar data',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    occurrences = []
    for timestamp, section in sections.items():
        try:
            event_date = datetime.fromtimestamp(int(timestamp), timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        for event in section.get('events', []):
            # The site's "show" type is its stable distinction between productions
            # and manually-added talks, tours, worship, promotions, and day openings.
            if event.get('type') != 'show':
                continue
            title = clean_text(event.get('name'))
            event_url = clean_text(event.get('link'))
            time_from = clean_text(event.get('start')) or None
            if title and event_url and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from or ''):
                occurrences.append({
                    'title': title,
                    'date': event_date,
                    'url': event_url,
                    'time_from': time_from,
                })
    return occurrences


def extract_venue(soup):
    headings = ' '.join(clean_text(node) for node in soup.select('.showtime-title'))
    if re.search(r'\bOlavinlinnassa\b', headings, re.IGNORECASE):
        return 'Olavinlinna'
    if re.search(r'\bSavonlinnasalissa\b', headings, re.IGNORECASE):
        return 'Savonlinnasali'
    return None


def extract_description(soup):
    content = soup.select_one('main.main-content') or soup.select_one('main')
    if not content:
        return None
    content = BeautifulSoup(str(content), 'html.parser')
    for node in content.select(
        '.showtimes-wrapper, .tickets-button-show-info, .buy, .ticket-price, '
        '.selected-events, script, style, noscript, picture, img'
    ):
        node.decompose()
    return clean_text(content) or None


def event_details(url):
    soup = get_soup(url)
    return extract_venue(soup), extract_description(soup)


def scrape_events():
    occurrences = []
    for url in calendar_urls():
        try:
            occurrences.extend(parse_calendar(get_soup(url), url))
        except requests.RequestException as error:
            log_message(
                'Failed to read Opera Festival calendar',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    details = {}
    event_urls = sorted({item['url'] for item in occurrences})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(event_details, url): url for url in event_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Opera Festival event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for item in occurrences:
        venue, description = details.get(item['url'], (None, None))
        if not venue:
            continue
        records.append({
            **item,
            'venue': venue,
            'city': CITY,
            'country_code': 'FI',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
    )


class OperaFestivalFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operafestival_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_events()


def main():
    OperaFestivalFiCrawler().run()


if __name__ == '__main__':
    main()
