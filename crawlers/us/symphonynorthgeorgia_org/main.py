import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.symphonynorthgeorgia.org/'
EVENTS_URL = f'{SOURCE_URL}events'
SITEMAP_URL = f'{SOURCE_URL}event-pages-sitemap.xml'
SOURCE = 'Symphony Orchestra of North Georgia'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(clean_text(value).replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def city_from_address(value):
    address = clean_text(value)
    match = re.search(r',\s*([^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*,?\s*USA$', address)
    return clean_text(match.group(1)) if match else ''


def event_description(soup):
    section = soup.select_one('[data-hook="about-section"]')
    if not section:
        return None
    heading = section.select_one('[data-hook="about"]')
    if heading:
        heading.decompose()
    description = clean_text(section.get_text('\n', strip=True))
    return description or None


def parse_event(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    data = event_data(soup)
    location = data.get('location') if isinstance(data.get('location'), dict) else {}

    title = clean_text(data.get('name'))
    event_date, time_from = parse_datetime(data.get('startDate'))
    venue = clean_text(location.get('name'))
    city = city_from_address(location.get('address'))

    # Some old Wix records use only the city as the location name. That is not
    # a valid venue, even when their street address remains available.
    if venue.casefold() == city.casefold():
        venue = ''

    if not all((title, event_date, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': event_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    return sorted({
        clean_text(node.get_text())
        for node in soup.find_all('loc')
        if '/event-details/' in clean_text(node.get_text())
    })


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class SymphonyNorthGeorgiaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symphonynorthgeorgia_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Symphony North Georgia event detail',
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
                        'Skipped incomplete Symphony North Georgia event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, or city is missing',
                    )

        if not records:
            log_message(
                'No parseable Symphony North Georgia events found',
                event='crawler_empty_listing',
                level='warning',
                url=EVENTS_URL,
                record_count=0,
            )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SymphonyNorthGeorgiaOrgCrawler().run()


if __name__ == '__main__':
    main()
