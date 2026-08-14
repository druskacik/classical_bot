import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera.wroclaw.pl/'
CALENDAR_URL = f'{SOURCE_URL}repertuar'
SOURCE = 'Opera Wrocławska'
DEFAULT_VENUE = 'Opera Wrocławska'
DEFAULT_CITY = 'Wrocław'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def archive_values(html):
    soup = BeautifulSoup(html, 'html.parser')
    return [
        option['value']
        for option in soup.select('select[name="archive"] option[value]')
        if option.get('value', '').isdigit()
    ]


def occurrence_from_url(url):
    value = parse_qs(urlsplit(url).query).get('date', [None])[0]
    if not value:
        return None, None
    try:
        occurrence = datetime.fromisoformat(value)
    except ValueError:
        return None, None
    return occurrence.date().isoformat(), occurrence.strftime('%H:%M')


def canonical_detail_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('.performance-item'):
        link = item.select_one('.performance-item__title a[href*="date="]')
        if not link:
            continue
        title = clean_text(link)
        url = link.get('href', '').strip()
        event_date, event_time = occurrence_from_url(url)
        category = clean_text(item.select_one('.performance-item__label'))

        # The tours are building visits, not performances. Other education and
        # choir entries remain candidates because some include live music.
        if not title or not event_date or category == 'Zwiedzanie Opery':
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            '_detail_url': canonical_detail_url(url),
        })
    return records


def parse_detail(html):
    soup = BeautifulSoup(html, 'html.parser')
    sections = [clean_text(node) for node in soup.select('.accordion__content.wysiwyg')]
    description = '\n\n'.join(dict.fromkeys(section for section in sections if section))
    return description or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_html = get_html(session, CALENDAR_URL)
    pages = [(CALENDAR_URL, first_html)]
    pages.extend(
        (f'{CALENDAR_URL}?archive={archive}', None)
        for archive in archive_values(first_html)
    )

    records = []
    for url, html in pages:
        try:
            records.extend(parse_calendar(html if html is not None else get_html(session, url)))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape repertoire page', event='crawler_page_failed', level='warning',
                url=url, error_type=type(error).__name__, error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['url'], record['date'], record['time_from'])
        unique[key] = record
    records = list(unique.values())

    descriptions = {}
    detail_urls = {record['_detail_url'] for record in records}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_html, session, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = parse_detail(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail', event='crawler_page_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error),
                )

    output = []
    for record in records:
        detail_url = record.pop('_detail_url')
        record.update({
            'venue': DEFAULT_VENUE,
            'city': DEFAULT_CITY,
            'country_code': 'PL',
            'description': descriptions.get(detail_url),
        })
        output.append(record)
    return sorted(output, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class OperaWroclawPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_wroclaw_pl',
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
    OperaWroclawPlCrawler().run()


if __name__ == '__main__':
    main()
