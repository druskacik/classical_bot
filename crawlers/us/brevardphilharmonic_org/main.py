import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://brevardphilharmonic.org/'
SOURCE = 'Brevard Philharmonic'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
CITY = 'Brevard'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?,\s+(20\d{2})\b',
    re.I,
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?', re.I)
SEASON_TITLE_RE = re.compile(r'^(?:20\d{2}-20\d{2}|concert archive|coming soon)$', re.I)


def clean_text(value, separator=' '):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(separator, strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        value = re.sub(r'[ \t]+', ' ', value)
        value = re.sub(r' *\n *', '\n', value)
        return re.sub(r'\n{3,}', '\n\n', value).strip()
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def api_items(session, post_type):
    page = 1
    items = []
    while True:
        response = session.get(
            f'{API_URL}/{post_type}',
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,content',
            },
            timeout=45,
        )
        response.raise_for_status()
        items.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return items
        page += 1


def current_record(item, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    title = clean_text(soup.select_one('h1')) or clean_text(item.get('title', {}).get('rendered'))
    date_node = soup.select_one('.text-concert-date')
    time_node = soup.select_one('.text-concert-time')
    event_date = parse_date(date_node)

    venue = ''
    if date_node:
        details_row = date_node.parent
        children = details_row.find_all(recursive=False)
        try:
            date_index = children.index(date_node)
        except ValueError:
            date_index = -1
        for child in children[date_index + 1:]:
            if child is time_node:
                continue
            candidate = clean_text(child.find('span', recursive=False) or child)
            if candidate and not TIME_RE.fullmatch(candidate):
                venue = candidate
                break

    description_parts = []
    subtitle = clean_text(soup.select_one('.text-concert-subtitle'))
    if subtitle:
        description_parts.append(subtitle)
    for selector in ('#divprogram-overview', '#div-program', '#div-program-notes'):
        section = soup.select_one(selector)
        if section:
            text = clean_text(section, '\n')
            text = re.sub(r'^(?:Concert Program|Program Notes)\s*', '', text, flags=re.I)
            if text and text not in description_parts:
                description_parts.append(text)

    if not all((title, event_date, item.get('link'), venue)):
        return None
    return make_record(
        title, event_date, item['link'], parse_time(time_node), venue,
        '\n\n'.join(description_parts) or None,
    )


def archive_venue(text, date_match):
    patterns = (
        r'Brevard-Davidson River Presbyterian Church',
        r'Whittington-Pfohl Auditorium',
        r'(?:the )?Candler(?:\'s)? Residence',
        r'The Porter Center',
        r'Porter Center',
    )
    nearby_text = text[max(0, date_match.start() - 250):date_match.end() + 350]
    for pattern in patterns:
        match = re.search(pattern, nearby_text, re.I)
        if match:
            return clean_text(match.group(0)).removeprefix('the ')
    return None


def archive_record(item):
    title = clean_text(item.get('title', {}).get('rendered'))
    if not title or SEASON_TITLE_RE.fullmatch(title):
        return None
    content = BeautifulSoup(item.get('content', {}).get('rendered', ''), 'html.parser')
    text = clean_text(content, '\n')
    date_match = DATE_RE.search(text)
    event_date = parse_date(date_match.group(0)) if date_match else None
    venue = archive_venue(text, date_match) if date_match else None
    if not all((event_date, venue, item.get('link'))):
        return None
    return make_record(title, event_date, item['link'], parse_time(text), venue, text or None)


def make_record(title, event_date, url, time_from, venue, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BrevardPhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brevardphilharmonic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
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
            current_items = api_items(session, 'bp-concert')
            archive_items = api_items(session, 'bp-concert-archive')
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Brevard Philharmonic catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for item in current_items:
            try:
                response = session.get(item['link'], timeout=45)
                response.raise_for_status()
                record = current_record(item, response.text)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Brevard Philharmonic concert',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=item.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records.extend(filter(None, (archive_record(item) for item in archive_items)))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    BrevardPhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
