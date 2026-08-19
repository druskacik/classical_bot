import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.msmnyc.edu/'
PERFORMANCES_URL = f'{SOURCE_URL}performances/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/performances'
SOURCE = 'Manhattan School of Music'
PAGE_SIZE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/json;q=0.9,*/*;q=0.8',
}

STATE_NAMES = {
    'new york': 'NY',
    'new jersey': 'NJ',
    'connecticut': 'CT',
}
LOCATION_RE = re.compile(
    r'\b([A-Za-z][A-Za-z .\'’-]+?),\s*'
    r'(New York|New Jersey|Connecticut|NY|NJ|CT)'
    r'(?:\s+\d{5}(?:-\d{4})?)?\b',
    re.I,
)
DATE_FORMATS = (
    '%b %d, %Y %I:%M %p',
    '%B %d, %Y %I:%M %p',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def clean_inline(value):
    return re.sub(r'\s+', ' ', clean_text(value)).strip()


def parse_datetime(value):
    # Some multi-performance pages show a range in the eyebrow. The first
    # timestamp is the concrete occurrence represented by the calendar post.
    first = re.split(r'\s+-\s+', clean_text(value), maxsplit=1)[0]
    first = re.sub(r'\s+', ' ', first).strip()
    for date_format in DATE_FORMATS:
        try:
            parsed = datetime.strptime(first, date_format)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            continue
    return None, None


def location_from_content(content):
    paragraphs = content.select('p')
    for paragraph in paragraphs:
        text = clean_text(paragraph)
        match = LOCATION_RE.search(text)
        if not match:
            continue

        city = match.group(1).strip(' ,')
        state = STATE_NAMES.get(match.group(2).lower(), match.group(2).upper())
        if state not in {'NY', 'NJ', 'CT'}:
            continue

        candidates = [
            line
            for node in paragraph.select('strong, b')
            for line in clean_text(node).splitlines()
        ]
        candidates = [
            value for value in candidates
            if value
            and value.lower() != SOURCE.lower()
            and not re.search(r'\b(?:tickets?|free|admission|price|sold out)\b', value, re.I)
            and not LOCATION_RE.search(value)
        ]
        venue = candidates[0] if candidates else ''

        if not venue:
            lines = clean_text(paragraph).splitlines()
            address_index = next(
                (index for index, line in enumerate(lines) if LOCATION_RE.search(line)),
                len(lines),
            )
            possible = [
                line for line in lines[:address_index]
                if line.lower() != SOURCE.lower()
                and not re.search(r'^\d+\s', line)
            ]
            venue = possible[0] if possible else ''

        if not venue and SOURCE.lower() in text.lower():
            venue = SOURCE
        if venue and city:
            return venue, city

    return None


def record_from_html(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('h2.leadText')
    date_node = soup.select_one('h1.smallEyebrow')
    content = soup.select_one('.main_content > .richTextModule.contentModule')
    title = clean_inline(title_node)
    date, time_from = parse_datetime(date_node)
    if not title or not date or not content:
        return None

    location = location_from_content(content)
    if not location:
        return None
    venue, city = location
    description = clean_text(content) or None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_performance_urls(session):
    urls = []
    page = 1
    total_pages = None
    while total_pages is None or page <= total_pages:
        response = session.get(
            API_URL,
            params={
                'per_page': PAGE_SIZE,
                'page': page,
                'orderby': 'date',
                'order': 'asc',
                '_fields': 'link',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError('Unexpected performances API response')
        urls.extend(item.get('link') for item in payload if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages') or page)
        page += 1
    return list(dict.fromkeys(urls))


def fetch_record(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return record_from_html(url, response.text)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch Manhattan School of Music performance',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = fetch_performance_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_record, url): url for url in urls}
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue'], item['url']): item
        for item in records
    }
    result = sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )
    if not result:
        log_message(
            'No valid Manhattan School of Music performances found',
            event='crawler_empty_listing',
            level='warning',
            url=PERFORMANCES_URL,
            record_count=0,
        )
    return result


class MsmnycEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='msmnyc_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MsmnycEduCrawler().run()


if __name__ == '__main__':
    main()
