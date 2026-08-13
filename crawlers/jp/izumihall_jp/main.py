import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.izumihall.jp/'
SOURCE = 'Sumitomolife Izumi Hall'
SCHEDULE_API = f'{SOURCE_URL}wp-json/wp/v2/schedule'
VENUE = 'Sumitomolife Izumi Hall'
CITY = 'Osaka'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def schedule_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            SCHEDULE_API,
            params={'per_page': 100, 'page': page, '_fields': 'id,link'},
            timeout=60,
        )
        response.raise_for_status()
        posts = response.json()
        urls.extend(post['link'] for post in posts if post.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def table_value(section, label):
    for row in section.select('.eventTable tr'):
        heading = row.find('th')
        if clean_text(heading) == label:
            return row.find('td')
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('.scheduleSingle .pageContent')
    if not event:
        return None

    title = clean_text(event.select_one('.mainSection .eventTitle h3')).replace('\n', ' ')
    datetime_node = table_value(event.select_one('.mainSection'), '日時')
    datetime_text = clean_text(datetime_node)
    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', datetime_text)
    if not title or not date_match:
        return None
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'開演\s*([0-2]?\d):([0-5]\d)', datetime_text)
    time_from = None
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    for label in ('出演者', '演奏曲目'):
        node = table_value(event.select_one('.mainSection'), label)
        text = clean_text(node)
        if text:
            description_parts.append(f'{label}\n{text}')
    for node in event.select('.section01 .eventInfo'):
        text = clean_text(node)
        if text:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'JP',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = schedule_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Izumi Hall schedule detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['url']
    ))


class IzumihallJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='izumihall_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
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
    IzumihallJpCrawler().run()


if __name__ == '__main__':
    main()
