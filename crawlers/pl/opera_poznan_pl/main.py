import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera.poznan.pl/pl/'
REPERTOIRE_URL = urljoin(SOURCE_URL, 'repertuar')
SOURCE = 'Teatr Wielki im. Stanisława Moniuszki w Poznaniu'
DEFAULT_CITY = 'Poznań'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), '%d.%m.%Y').date().isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value or '')
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def detail_url(href):
    url = urljoin(SOURCE_URL, href)
    parsed = urlparse(url)
    if parsed.netloc == urlparse(SOURCE_URL).netloc and not parsed.path.startswith('/pl/'):
        return urljoin(SOURCE_URL, parsed.path.lstrip('/'))
    return url


def parse_repertoire(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('.reportaile-item--rep-list'):
        link = item.select_one('a.reportaile-item__name[href]')
        title = clean_text(item.select_one('.reportaile-item__name__title'))
        event_date = parse_date(clean_text(item.select_one('.reportaile-item__date__title')))
        venue = clean_text(item.select_one('.reportaile-item__place .place-name'))
        if not venue:
            place = item.select_one('.reportaile-item__place__item[data-tippy-content]')
            venue = clean_text(place.get('data-tippy-content')) if place else ''
        if not link or not title or not event_date or not venue:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': detail_url(link['href']),
            'time_from': parse_time(clean_text(item.select_one('.reportaile-item__date__info'))),
            'venue': venue,
            'city': DEFAULT_CITY,
            'country_code': 'PL',
            'description': None,
        })

    unique = {}
    for record in records:
        key = (record['url'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    return list(unique.values())


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    for selector in ('.spectatle-header__info__short', '.information-item__text'):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def get_concerts():
    records = parse_repertoire(get_page(REPERTOIRE_URL))
    descriptions = {}

    def load_description(url):
        try:
            return url, parse_description(get_page(url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape event detail',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return url, None

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(load_description, url) for url in {row['url'] for row in records}]
        for future in as_completed(futures):
            url, description = future.result()
            descriptions[url] = description

    for record in records:
        record['description'] = descriptions.get(record['url'])
    return sorted(
        records,
        key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
    )


class OperaPoznanPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_poznan_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaPoznanPlCrawler().run()


if __name__ == '__main__':
    main()
