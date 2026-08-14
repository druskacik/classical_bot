import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://beirutchants.com/'
SOURCE = 'Beirut Chants Festival'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/bc-event'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
DATE_PATTERN = re.compile(
    r'\b(?:MON|TUE|WED|THU|FRI|SAT|SUN)\.?\s*'
    r'([A-Z]{3})\s+(\d{1,2}),\s+(\d{4})'
    r'(?:\s*\|\s*(\d{1,2}):(\d{2}))?',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    text = html.unescape(str(value))
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update(HEADERS)
    return session


def discover_event_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, '_fields': 'link'},
            timeout=45,
        )
        response.raise_for_status()
        items = response.json()
        urls.extend(item['link'] for item in items if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def infer_city(venue):
    folded = venue.casefold()
    if 'baabda' in folded:
        return 'Baabda'
    # The festival's calendar is based in Beirut. Other location fragments in
    # the feed (Monot, Kantari, Gemmayzeh and AUB) are Beirut neighborhoods or
    # institutions, and many venue names explicitly end in Beirut.
    return 'Beirut'


def parse_event(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    event = soup.select_one('.bc-event')
    if not event:
        return None

    lines = [clean_text(line) for line in event.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    title_node = event.select_one('.elementor-widget-theme-post-title h1, '
                                  '.elementor-widget-theme-post-title h2')
    title = clean_text(title_node) or (lines[0] if lines else '')

    date_index = None
    match = None
    for index, line in enumerate(lines):
        match = DATE_PATTERN.search(line)
        if match:
            date_index = index
            break
    if not title or match is None or date_index is None:
        return None

    try:
        event_date = datetime.strptime(
            f'{match.group(2)} {match.group(1)} {match.group(3)}', '%d %b %Y'
        ).date().isoformat()
    except ValueError:
        return None

    time_from = None
    if match.group(4):
        hour = int(match.group(4))
        minute = int(match.group(5))
        if hour > 23 or minute > 59:
            return None
        time_from = f'{hour:02d}:{minute:02d}'

    venue = lines[date_index + 1] if date_index + 1 < len(lines) else ''
    if not venue or venue.casefold() in {'event artists', "artist's biography", "event's program"}:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': infer_city(venue),
        'country_code': 'LB',
        'description': clean_text(event) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = make_session()
    records = []
    for url in discover_event_urls(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_event(response.text, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping Beirut Chants event without complete occurrence data',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Beirut Chants event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BeirutChantsComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='beirutchants_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LB',
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
    BeirutChantsComCrawler().run()


if __name__ == '__main__':
    main()
