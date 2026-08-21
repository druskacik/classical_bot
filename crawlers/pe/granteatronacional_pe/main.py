import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://granteatronacional.pe/'
SOURCE = 'Gran Teatro Nacional'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario/{month}')
ARCHIVE_START_YEAR = 2018
VENUE = 'Gran Teatro Nacional'
CITY = 'Lima'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-PE,es;q=0.9',
}

# GTN is a mixed performing-arts venue. These are the first-party labels which
# can contain an eligible concert, opera, ballet, crossover, or family event.
# They are intentionally sent to the potential-event classifier: labels such
# as Musica and Danza also contain salsa, jazz, folk, and nonclassical dance.
CANDIDATE_CATEGORIES = {
    'accesible',
    'danza',
    'en familia',
    'especial',
    'musica',
    'opera / lirica',
    'proximamente',
    '¡es gratis!',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def normalized_label(value):
    translation = str.maketrans('ÁÉÍÓÚÜÑ', 'AEIOUUN')
    return clean_text(value).upper().translate(translation).casefold()


def month_range():
    """Return archive months through a modest future scheduling horizon."""
    today = date.today()
    end_year = today.year + 2
    return [
        f'{year}{month:02d}'
        for year in range(ARCHIVE_START_YEAR, end_year + 1)
        for month in range(1, 13)
        if (year, month) >= (ARCHIVE_START_YEAR, 1)
    ]


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    if 'The URL you requested has been blocked' in response.text:
        raise requests.RequestException('The source blocked the requested URL')
    return response.text


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('.view-item'):
        contents = item.select_one('.contents')
        link = contents.select_one('.title a[href]') if contents else None
        start = contents.select_one('time[datetime]') if contents else None
        category = contents.select_one('.category-div') if contents else None
        if not link or not start or not category:
            continue

        start_value = clean_text(start.get('datetime')).removesuffix('Z')
        try:
            event_date, event_time = start_value.split('T', 1)
        except ValueError:
            continue
        event_time = event_time[:5]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', event_date):
            continue
        try:
            date.fromisoformat(event_date)
        except ValueError:
            continue

        category_text = clean_text(category)
        if normalized_label(category_text) not in CANDIDATE_CATEGORIES:
            continue
        title = clean_text(link)
        url = urljoin(SOURCE_URL, link.get('href'))
        if not title or not url:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time if re.fullmatch(r'\d{2}:\d{2}', event_time) else None,
            'venue': VENUE,
            'city': CITY,
            'country_code': 'PE',
            '_category': category_text,
        })
    return records


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('.formato')
    if not content:
        return None

    # The second direct div contains the schedule and long editorial body. The
    # later sibling contains ticket prices and discounts, which are excluded.
    body_blocks = [child for child in content.find_all('div', recursive=False)
                   if 'zaumentar' not in (child.get('class') or [])]
    if not body_blocks:
        return None
    description = clean_text(body_blocks[0])
    return description or None


def fetch_calendar(session, month):
    url = CALENDAR_URL.format(month=month)
    return parse_calendar(get_page(session, url))


def fetch_description(session, url):
    return parse_description(get_page(session, url))


class GranTeatroNacionalPeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='granteatronacional_pe',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[
            ('source_url', SOURCE_URL),
            ('source', SOURCE),
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_calendar, session, month): month
                for month in month_range()
            }
            for future in as_completed(futures):
                month = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch GTN calendar month',
                        event='crawler_page_failed',
                        level='warning',
                        url=CALENDAR_URL.format(month=month),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        # One detail request per production page, even when it has many dates.
        descriptions = {}
        urls = sorted({record['url'] for record in records})
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_description, session, url): url
                for url in urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch GTN event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    descriptions[url] = None

        for record in records:
            record['description'] = descriptions.get(record['url'])
            record.pop('_category', None)
        unique_records = {
            (record['title'], record['date'], record['time_from'], record['venue']): record
            for record in records
        }
        return sorted(
            unique_records.values(),
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    GranTeatroNacionalPeCrawler().run()


if __name__ == '__main__':
    main()
