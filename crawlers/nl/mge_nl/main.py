import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mge.nl/'
FEED_URL = f'{SOURCE_URL}event_feed_json/'
SOURCE = 'Muziekgebouw Eindhoven'
DEFAULT_CITY = 'Eindhoven'
DEFAULT_VENUE = 'Muziekgebouw Eindhoven'
SITEMAP_URLS = [
    f'{SOURCE_URL}event-sitemap.xml',
    f'{SOURCE_URL}event-sitemap2.xml',
    f'{SOURCE_URL}event-sitemap3.xml',
]
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}
MONTHS = {
    'jan': 1, 'feb': 2, 'mrt': 3, 'apr': 4, 'mei': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def fetch(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def get_feed(session):
    payload = fetch(session, FEED_URL).json()
    events = payload.get('events')
    if not isinstance(events, dict):
        raise ValueError('First-party event feed has no events object')
    return events


def archive_urls(session, current_urls):
    urls = set()
    for sitemap_url in SITEMAP_URLS:
        try:
            soup = BeautifulSoup(fetch(session, sitemap_url).content, 'xml')
        except requests.RequestException as error:
            log_message(
                'Failed to fetch event sitemap', event='crawler_page_failed',
                level='warning', url=sitemap_url,
                error_type=type(error).__name__, error_message=str(error),
            )
            continue
        for node in soup.select('loc'):
            url = clean_text(node)
            path = urlparse(url).path.rstrip('/')
            if re.fullmatch(r'/agenda/[^/]+', path) and url not in current_urls:
                urls.add(url)
    return sorted(urls)


def detail_description(soup):
    parts = []
    for node in soup.select('.strip-text--event .wysiwyg, .strip-text--event'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def detail_fields(session, url):
    soup = BeautifulSoup(fetch(session, url).text, 'html.parser')
    description = detail_description(soup)
    title_node = soup.select_one('h1.heading-l, h1')
    title = clean_text(title_node)
    hero = soup.select_one('.hero-text')
    venue_node = hero.select_one('.tile-event__location') if hero else None
    venue = clean_text(venue_node) or DEFAULT_VENUE
    return soup, title, venue, description


def current_records(event, url, title, venue, description):
    records = []
    title = title or clean_text(event.get('title'))
    venue_from_page = venue or DEFAULT_VENUE
    for occurrence in event.get('times') or []:
        raw_start = str(occurrence.get('program_start') or '')
        try:
            start = datetime.strptime(raw_start, '%Y%m%d%H%M')
        except ValueError:
            continue
        occurrence_venue = clean_text(occurrence.get('location')) or venue_from_page
        if not title or not occurrence_venue:
            continue
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': occurrence_venue,
            'city': DEFAULT_CITY,
            'country_code': 'NL',
            'description': description or clean_text(event.get('intro')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def archive_record(session, url):
    soup, title, venue, description = detail_fields(session, url)
    hero = soup.select_one('.hero-text')
    if not hero or not title or not venue:
        return None
    times = [clean_text(node) for node in hero.select('time')]
    date_text = next((value for value in times if re.search(r'\b\d{4}\b', value)), '')
    match = re.search(r'\b(\d{1,2})\s+([a-z]{3})\s+(\d{4})\b', date_text.lower())
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        event_date = datetime(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).date().isoformat()
    except ValueError:
        return None
    time_from = next((value for value in times if re.fullmatch(r'\d{1,2}:\d{2}', value)), None)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': DEFAULT_CITY,
        'country_code': 'NL',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_current_detail(session, event):
    url = event.get('permalink')
    if not url:
        return []
    _, title, venue, description = detail_fields(session, url)
    return current_records(event, url, title, venue, description)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = get_feed(session)
    current_urls = {event.get('permalink') for event in events.values() if event.get('permalink')}
    archives = archive_urls(session, current_urls)
    records = []
    jobs = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        jobs.update({
            executor.submit(scrape_current_detail, session, event): event.get('permalink')
            for event in events.values()
        })
        jobs.update({executor.submit(archive_record, session, url): url for url in archives})
        for future in as_completed(jobs):
            url = jobs[future]
            try:
                result = future.result()
                if isinstance(result, list):
                    records.extend(result)
                elif result:
                    records.append(result)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail', event='crawler_item_failed',
                    level='warning', url=url, error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue'], item['url']
    ))


class MgeNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mge_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MgeNlCrawler().run()


if __name__ == '__main__':
    main()
