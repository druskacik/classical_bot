import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.minnesotaorchestra.org/'
FEED_URL = urljoin(SOURCE_URL, 'api/event-feed/7')
SOURCE = 'Minnesota Orchestra'
DEFAULT_CITY = 'Minneapolis'
DEFAULT_VENUE = 'Orchestra Hall'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_html(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n *|\n{3,}', '\n', text).strip()


def detail_program(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = []
        for item in soup.select('.program__item'):
            text = clean_html(item.get_text('\n', strip=True))
            if text:
                items.append(text)
        return '\n'.join(items)
    except requests.RequestException as error:
        log_message(
            'Could not enrich event program',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return ''


def location_for(item, description):
    web_contents = item.get('web_contents') or {}
    venue = clean_html(web_contents.get('WebVenueOverride'))
    facility = clean_html(item.get('facility_title'))

    if item.get('facility_no') == 221:
        return venue or facility, 'Brainerd'
    if (venue or '').lower() == 'landmark center':
        return venue, 'Saint Paul'
    if facility.lower() == 'general admission':
        return DEFAULT_VENUE, DEFAULT_CITY
    return venue or facility or DEFAULT_VENUE, DEFAULT_CITY


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(
        FEED_URL,
        headers={'Accept': 'application/json', 'Referer': urljoin(SOURCE_URL, 'tickets/calendar')},
        timeout=45,
    )
    response.raise_for_status()
    events = response.json()

    urls = sorted({urljoin(SOURCE_URL, item.get('event_page_url', '')) for item in events})
    programs = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(detail_program, url): url for url in urls}
        for future in as_completed(futures):
            programs[futures[future]] = future.result()

    records = []
    for item in events:
        title = clean_html(item.get('title'))
        url = urljoin(SOURCE_URL, item.get('event_page_url', ''))
        description = clean_html(item.get('description'))
        program = programs.get(url, '')
        if program and program not in description:
            description = f'{description}\n\nPROGRAM\n{program}'.strip()

        venue, city = location_for(item, description)
        try:
            performance = datetime.fromisoformat(item.get('perf_date', ''))
            event_date = performance.date().isoformat()
            time_from = performance.strftime('%H:%M')
        except (TypeError, ValueError):
            continue

        if not all((title, url.startswith(('http://', 'https://')), venue, city)):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No event performances found',
            event='crawler_empty_listing',
            level='warning',
            url=FEED_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class MinnesotaOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='minnesotaorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MinnesotaOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
