import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ludlow-arts-classical.org.uk/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Ludlow Arts: Classical'
CITY = 'Ludlow'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    r'(\d{1,2})\s+'
    r'(January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE)
EVENT_PATH_RE = re.compile(r'^/\d{6}[a-z]?\.html$', re.IGNORECASE)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text, url=None):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        parsed = datetime.strptime(' '.join(match.groups()), '%d %B %Y').date()
    except ValueError:
        return None

    # Event filenames encode YYMMDD. Prefer that date when a hand-authored
    # month typo conflicts with it (the page's stated weekday also agrees).
    if url:
        filename_match = re.match(r'^(\d{6})', urlparse(url).path.rsplit('/', 1)[-1])
        if filename_match:
            try:
                encoded = datetime.strptime(filename_match.group(1), '%y%m%d').date()
                weekday = text[:match.start()].strip().split()[-1].lower()
                if parsed != encoded and encoded.strftime('%A').lower() == weekday:
                    parsed = encoded
            except (ValueError, IndexError):
                pass
    return parsed.isoformat()


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def event_url_from_box(box):
    for link in box.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if EVENT_PATH_RE.match(urlparse(url).path):
            return url
    return SOURCE_URL


def record(title, event_date, url, time_from, venue, description):
    if not title or not event_date or not venue:
        return None
    if venue == 'Ludlow Assmbly Rooms':
        venue = 'Ludlow Assembly Rooms'
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_homepage(content):
    soup = BeautifulSoup(content, 'html.parser')
    records = []
    detail_urls = []
    for box in soup.select('.content-box'):
        paragraphs = box.select(':scope > p')
        date_index = next(
            (index for index, node in enumerate(paragraphs) if DATE_RE.search(clean_text(node))),
            None,
        )
        if date_index is None or len(paragraphs) <= date_index + 3:
            continue
        url = event_url_from_box(box)
        event_date = parse_date(clean_text(paragraphs[date_index]), url)
        time_from = parse_time(clean_text(paragraphs[date_index + 1]))
        venue = clean_text(paragraphs[date_index + 2])
        title = clean_text(paragraphs[date_index + 3])
        body = [
            clean_text(node)
            for node in paragraphs[date_index + 3:]
            if 'footer' not in (node.get('class') or [])
        ]
        description = '\n\n'.join(part for part in body if part)
        item = record(title, event_date, url, time_from, venue, description)
        if item:
            records.append(item)
            if url != SOURCE_URL:
                detail_urls.append(url)
    return records, detail_urls


def parse_detail(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    content_box = soup.select_one('.content-box')
    title = clean_text(soup.select_one('.container-page-heading'))
    if not content_box or not title:
        return None
    paragraphs = content_box.select(':scope > p')
    date_index = next(
        (index for index, node in enumerate(paragraphs) if DATE_RE.search(clean_text(node))),
        None,
    )
    if date_index is None or len(paragraphs) <= date_index + 2:
        return None
    event_date = parse_date(clean_text(paragraphs[date_index]), url)
    time_from = parse_time(clean_text(paragraphs[date_index + 1]))
    venue = clean_text(paragraphs[date_index + 2])
    description_parts = []
    for node in paragraphs[date_index + 3:]:
        text = clean_text(node)
        if not text or re.search(r'\b(?:tickets?|free entry|donations?)\b', text, re.IGNORECASE):
            continue
        description_parts.append(text)
    return record(
        title,
        event_date,
        url,
        time_from,
        venue,
        '\n\n'.join(description_parts),
    )


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


class LudlowArtsClassicalOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ludlow_arts_classical_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            homepage = get_response(session, SOURCE_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Ludlow Arts classical calendar',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        homepage_records, homepage_detail_urls = parse_homepage(homepage.content)
        detail_urls = list(homepage_detail_urls)
        try:
            sitemap = get_response(session, SITEMAP_URL)
            sitemap_soup = BeautifulSoup(sitemap.content, 'xml')
            detail_urls.extend(
                clean_text(node)
                for node in sitemap_soup.select('url > loc')
                if EVENT_PATH_RE.match(urlparse(clean_text(node)).path)
            )
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Ludlow Arts sitemap; using homepage events',
                event='crawler_sitemap_failed',
                level='warning',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        records_by_url = {
            item['url']: item for item in homepage_records if item['url'] != SOURCE_URL
        }
        records_without_detail = [
            item for item in homepage_records if item['url'] == SOURCE_URL
        ]
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(get_response, session, url): url
                for url in dict.fromkeys(detail_urls)
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    item = parse_detail(future.result().content, url)
                    if item:
                        records_by_url[url] = item
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Ludlow Arts event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            [*records_by_url.values(), *records_without_detail],
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    LudlowArtsClassicalOrgUkCrawler().run()


if __name__ == '__main__':
    main()
