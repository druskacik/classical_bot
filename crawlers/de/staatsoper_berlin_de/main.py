import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.staatsoper-berlin.de/de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'spielplan/')
SOURCE = 'Staatsoper Unter den Linden'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = text.replace('\u00ad', '').replace('\u00a0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_pages(session):
    """Follow the calendar's own links across every published date page."""
    pending = [SCHEDULE_URL]
    seen = set()
    while pending:
        url = pending.pop()
        if url in seen:
            continue
        seen.add(url)
        try:
            soup = get_soup(session, url)
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            # The last published page currently retains a Next link whose
            # target is a normal 404 rather than an empty calendar page.
            if status == 404 and url != SCHEDULE_URL:
                continue
            raise
        yield soup

        # The current page exposes Next and, when retained by the site,
        # Previous. Following both includes still-published past performances.
        for link in soup.select('#next-link[href], #previous-link[href]'):
            next_url = urljoin(SOURCE_URL, link.get('href'))
            if next_url not in seen:
                pending.append(next_url)


def resolve_city(venue_node):
    venue = clean_text(venue_node.get_text(' ', strip=True))
    link = venue_node.select_one('a[href]')
    href = link.get('href', '') if link else ''
    lower = venue.lower()

    # The institution's own location pages describe Berlin venues. Partner
    # venues such as the Philharmonie state Berlin directly in their names.
    if '/de/ihr-besuch/anfahrt/' in href or 'berlin' in lower:
        return venue, 'Berlin'
    return None, None


def listing_record(article):
    title_link = article.select_one('.termin__title a[href]')
    time_node = article.select_one('.termin__meta time[datetime]')
    venue_node = article.select_one('.termin__spielstaette')
    if not all((title_link, time_node, venue_node)):
        return None

    title = clean_text(title_link.get_text(' ', strip=True))
    raw_datetime = (time_node.get('datetime') or '').strip()
    match = re.match(r'^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2}))?', raw_datetime)
    venue, city = resolve_city(venue_node)
    if not title or not match or not venue or not city:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    url = urljoin(SOURCE_URL, title_link.get('href'))
    if not urlparse(url).fragment:
        event_id = article.get('data-termin-id')
        if event_id:
            url = f'{url}#event-{event_id}'

    description_parts = []
    work_info = clean_text(article.select_one('.termin__werkinfo'))
    if work_info:
        description_parts.append(work_info)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': (
            f'{match.group(2)}:{match.group(3)}' if match.group(2) else None
        ),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': '\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    for selector, heading in (('#info .wysiwyg', None), ('#programm', 'Programm')):
        node = soup.select_one(selector)
        value = clean_text(node)
        if not value:
            continue
        if heading and not value.lower().startswith(heading.lower()):
            value = f'{heading}\n{value}'
        if value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records_by_key = {}
    for soup in calendar_pages(session):
        for article in soup.select('article.termin-list__item'):
            record = listing_record(article)
            if record:
                key = (
                    record['title'], record['date'], record['time_from'],
                    record['venue'],
                )
                records_by_key[key] = record

    records = list(records_by_key.values())
    detail_urls = {urldefrag(record['url']).url for record in records}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, url): url
            for url in detail_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(urldefrag(record['url']).url)
        fallback = record['description']
        if detail and fallback and fallback not in detail:
            record['description'] = f'{fallback}\n\n{detail}'
        else:
            record['description'] = detail or fallback

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class StaatsoperBerlinDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='staatsoper_berlin_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        # The opera calendar also includes talks, tours, and open-house events.
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
    StaatsoperBerlinDeCrawler().run()


if __name__ == '__main__':
    main()
