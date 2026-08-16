import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musicatmenlo.org/'
SOURCE = 'Music@Menlo'
EVENTS_URL = urljoin(SOURCE_URL, 'events/')
ARCHIVE_API = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
CITY = 'Atherton'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    normalized = re.sub(r'\s+', ' ', value).strip()
    for pattern in ('%b %d %Y', '%B %d %Y'):
        try:
            return datetime.strptime(normalized, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def event_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    return {
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('article a[href*="/event/"]')
    }


def archive_links(session):
    links = set()
    page = 1
    max_pages = 1
    while page <= max_pages:
        response = session.post(
            ARCHIVE_API,
            data={
                'action': 'filter_past_events',
                'year': '',
                'category': '',
                'search': '',
                'page': page,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get('success') or not isinstance(payload.get('data'), dict):
            raise ValueError(f'Unexpected archive response on page {page}')
        data = payload['data']
        links.update(event_links(data.get('html') or ''))
        max_pages = int(data.get('max_pages') or 1)
        page += 1
    return links


def booking_details(soup):
    for block in soup.select('.c-event-content .pt-5.pb-7'):
        venue_label = next(
            (node for node in block.find_all('span') if clean_text(node) == 'Venue:'),
            None,
        )
        text = clean_text(block)
        date_match = re.search(
            r'\b([A-Z][a-z]{2,8}\s+\d{1,2}\s+20\d{2})\b', text
        )
        time_match = re.search(r'\b(1[0-2]|[1-9]):([0-5]\d)\s*([AP]M)\b', text)
        event_date = parse_date(date_match.group(1)) if date_match else None
        time_from = None
        if time_match:
            time_from = datetime.strptime(
                ''.join(time_match.groups()), '%I%M%p'
            ).strftime('%H:%M')
        venue = ''
        if venue_label is not None:
            venue = clean_text(venue_label.find_next_sibling('span'))
        elif time_match:
            # Free festival events render the location inline as
            # "11:00 AM, Martin Family Hall" rather than with a Venue label.
            inline = re.search(
                r'\b(?:1[0-2]|[1-9]):[0-5]\d\s*[AP]M,\s*([^\n]+)', text
            )
            venue = inline.group(1).strip() if inline else ''
        if event_date and venue:
            return event_date, time_from, venue
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = re.sub(r'\s+', ' ', clean_text(soup.select_one('.c-event-hero h1'))).strip()
    details = booking_details(soup)
    if not title or not details:
        return None

    event_date, time_from, venue = details
    description = clean_text(soup.select_one('.c-event-content__description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Stanford' if 'bing concert hall' in venue.lower() else CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url)


class MusicAtMenloOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicatmenlo_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(EVENTS_URL, timeout=45)
            response.raise_for_status()
            links = event_links(response.text)
            links.update(archive_links(session))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Music@Menlo event listings',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_record, url): url for url in links}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Music@Menlo event detail',
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
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MusicAtMenloOrgCrawler().run()


if __name__ == '__main__':
    main()
