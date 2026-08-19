import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sinfoniaviva.co.uk/'
SOURCE = 'Sinfonia Viva'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(max_retries=Retry(
            total=2,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )),
    )
    return session


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    urls = []
    page = 1
    while True:
        response = get_response(
            session,
            EVENTS_API,
            params={'per_page': 100, 'page': page, '_fields': 'link'},
        )
        payload = response.json()
        urls.extend(item.get('link') for item in payload if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def detail_value(details, heading):
    if not details:
        return ''
    for node in details.select('h6'):
        if clean_text(node).casefold() == heading.casefold():
            return clean_text(node.find_next_sibling('p'))
    return ''


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*([ap]m)', clean_text(value), re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    event = soup.select_one('.event_single_page')
    intro = event.select_one('.intro') if event else None
    details = event.select_one('.event_details') if event else None

    title = clean_text(intro.select_one('h1')) if intro else ''
    city = clean_text(intro.select_one('.season')) if intro else ''
    date_text = detail_value(details, 'When')
    venue = detail_value(details, 'Where')

    try:
        event_date = datetime.strptime(date_text, '%A %d %B %Y').date().isoformat()
    except (TypeError, ValueError):
        return None

    time_text = ''
    when_node = next(
        (node for node in details.select('h6') if clean_text(node).casefold() == 'when'),
        None,
    ) if details else None
    if when_node:
        date_node = when_node.find_next_sibling('p')
        time_node = date_node.find_next_sibling('p') if date_node else None
        time_text = clean_text(time_node)

    description = clean_text(event.select_one('.event_description')) if event else ''
    if not title or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_text),
        'venue': venue,
        'city': city,
        'description': description or None,
    }


def scrape_concerts():
    session = make_session()
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result().content, url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped Sinfonia Viva event with incomplete details',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                    )
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Sinfonia Viva event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda row: (
        row['date'], row['time_from'] or '', row['title'], row['venue']
    ))


class SinfoniaVivaCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfoniaviva_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SinfoniaVivaCoUkCrawler().run()


if __name__ == '__main__':
    main()
