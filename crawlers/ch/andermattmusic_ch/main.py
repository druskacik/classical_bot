import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://andermattmusic.ch/de/'
EVENTS_API = 'https://andermattmusic.ch/de/wp-json/wp/v2/event'
SOURCE = 'Andermatt Music'
VENUE = 'Andermatt Konzerthalle'
CITY = 'Andermatt'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}

DATE_RE = re.compile(r'\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b')
TIME_RE = re.compile(r'\b(\d{1,2})[.:](\d{2})\s*(?:Uhr)?\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def listing_events(session):
    events = []
    page = 1
    while True:
        response = get_response(
            session,
            EVENTS_API,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'asc',
                '_fields': 'id,link,title',
            },
        )
        batch = response.json()
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return events


def detail_description(soup):
    parts = []
    started = False
    for widget in soup.select('.elementor-widget-heading, .elementor-widget-text-editor'):
        text = clean_text(widget)
        if not text:
            continue
        heading = widget.select_one('h1, h2, h3, h4, h5, h6')
        if heading and heading.name == 'h1':
            started = True
            continue
        if not started:
            continue
        if heading and text.casefold() in {'abonnements', 'über uns', 'faq'}:
            break
        if DATE_RE.search(text) or TIME_RE.fullmatch(text):
            continue
        if text.casefold().startswith(('preise:', 'tickets:', 'ticket:')):
            continue
        if text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def parse_event(event, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    heading = soup.select_one('h1')
    title = clean_text(heading) or clean_text((event.get('title') or {}).get('rendered'))
    url = event.get('link') or ''

    date_match = None
    time_match = None
    for widget in soup.select('.elementor-widget-text-editor'):
        text = clean_text(widget)
        if date_match is None:
            date_match = DATE_RE.search(text)
            if date_match:
                continue
        if date_match is not None and time_match is None:
            time_match = TIME_RE.search(text)
            if time_match:
                break

    if not title or not url or not date_match:
        return None
    try:
        event_date = date(
            int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1))
        ).isoformat()
    except ValueError:
        return None

    time_from = None
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            time_from = f'{hour:02d}:{minute:02d}'

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'CH',
        'description': detail_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(
            max_retries=Retry(
                total=4,
                backoff_factor=1,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=('GET',),
            )
        ),
    )
    events = listing_events(session)
    records = []

    # The WordPress host rate-limits larger bursts of detail-page requests.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(get_response, session, event['link']): event
            for event in events
            if event.get('link')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                record = parse_event(event, future.result().text)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
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


class AndermattMusicChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='andermattmusic_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
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
    AndermattMusicChCrawler().run()


if __name__ == '__main__':
    main()
