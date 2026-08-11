from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.conservatoire-nice.org/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda/')
SOURCE = 'Conservatoire de Nice'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}


def clean_text(element):
    if not element:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def clean_city(value):
    return re.sub(r'\s+cedex(?:\s+\d+)?$', '', value, flags=re.IGNORECASE).strip()


def get_soup(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.single--event')
    if not article:
        return []

    title = clean_text(article.select_one('h1.entry-title'))
    place = article.select_one('[itemprop="location"][itemtype="https://schema.org/Place"]')
    venue_meta = place.select_one('meta[itemprop="name"]') if place else None
    venue = (venue_meta.get('content') or '').strip() if venue_meta else ''
    city = clean_city(clean_text(place.select_one('[itemprop="addressLocality"]'))) if place else ''
    description = clean_text(article.select_one('.entry-content')) or None

    if not title or not venue or not city:
        return []

    records = []
    for schedule in article.select('[itemprop="eventSchedule"]'):
        date_meta = schedule.select_one('meta[itemprop="startDate"]')
        event_date = (date_meta.get('content') or '').strip() if date_meta else ''
        if not event_date:
            continue
        try:
            date.fromisoformat(event_date)
        except ValueError:
            continue

        time_meta = schedule.select_one('meta[itemprop="startTime"]')
        time_from = (time_meta.get('content') or '').strip()[:5] if time_meta else None
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from or None,
            'venue': venue,
            'city': city,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url)


def discover_event_urls():
    root = get_soup(AGENDA_URL)
    month_urls = {AGENDA_URL}
    for option in root.select('#month-filter option[data-url]'):
        month_urls.add(urljoin(SOURCE_URL, option.get('data-url', '')))

    event_urls = set()
    pages = [(month_url, event_type) for month_url in month_urls for event_type in ('concert', 'ballet')]
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, month_url, {'event_type': event_type}): month_url
            for month_url, event_type in pages
        }
        for future in as_completed(futures):
            month_url = futures[future]
            try:
                soup = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to load Conservatoire de Nice agenda page',
                    event='crawler_page_failed', level='warning', url=month_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            for article in soup.select('#event-list article[itemtype="https://schema.org/Event"]'):
                url_meta = article.select_one('meta[itemprop="url"]')
                if url_meta and url_meta.get('content'):
                    event_urls.add(urljoin(SOURCE_URL, url_meta['content']))
    return event_urls


class ConservatoireNiceOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='conservatoire_nice_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        event_urls = discover_event_urls()
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in event_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_records = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Conservatoire de Nice event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if not detail_records:
                    log_message(
                        'Skipped incomplete Conservatoire de Nice event',
                        event='crawler_item_skipped', level='warning', url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, or city is missing',
                    )
                records.extend(detail_records)
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ConservatoireNiceOrgCrawler().run()


if __name__ == '__main__':
    main()
