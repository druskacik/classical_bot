import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://alnwickmusicsociety.co.uk/'
SOURCE = 'Alnwick Music Society'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
CONCERT_CATEGORY_IDS = '36,37'  # Archived and Alnwick Playhouse
CITY = 'Alnwick'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_TIME_RE = re.compile(
    rf'\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+)?'
    rf'(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})\s+(20\d{{2}})'
    rf'(?:\s+at\s+(\d{{1,2}})(?:[.:](\d{{2}}))?\s*(am|pm))?',
    re.IGNORECASE,
)
TITLE_DATE_RE = re.compile(r'\s*[-–—]?\s*\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})\s*$')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def event_posts(session):
    posts = []
    page = 1
    while True:
        response = get_response(
            session,
            API_URL,
            params={
                'categories': CONCERT_CATEGORY_IDS,
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,categories',
            },
        )
        posts.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return posts


def parse_date_time(text):
    match = DATE_TIME_RE.search(text)
    if not match:
        return None, None

    day, month, year, hour, minute, meridiem = match.groups()
    try:
        event_date = datetime.strptime(f'{day} {month} {year}', '%d %B %Y').date().isoformat()
    except ValueError:
        return None, None

    if not hour:
        return event_date, None
    hour_value = int(hour)
    minute_value = int(minute or 0)
    if hour_value not in range(1, 13) or minute_value > 59:
        return event_date, None
    if meridiem.lower() == 'pm' and hour_value != 12:
        hour_value += 12
    elif meridiem.lower() == 'am' and hour_value == 12:
        hour_value = 0
    return event_date, f'{hour_value:02d}:{minute_value:02d}'


def extract_venue(lines, date_line_index):
    for line in lines[date_line_index + 1:date_line_index + 5]:
        candidate = clean_text(line)
        if not candidate or re.search(r'\b(?:Adults?|Tickets?|Book)\b', candidate, re.I):
            continue
        venue = candidate.split(',', 1)[0].strip(' .–-')
        if venue and venue.lower() != CITY.lower():
            return venue
    return None


def parse_event(content, post):
    soup = BeautifulSoup(content, 'html.parser')
    entry = soup.select_one('article .entry-content, .entry-content')
    if not entry:
        return None

    for node in entry.find_all(string=lambda value: clean_text(value) == 'Concert Dates'):
        sidebar = node.find_parent(class_=lambda value: value and 'et_pb_column' in value)
        if sidebar:
            sidebar.decompose()

    text = clean_text(entry)
    lines = [line for line in text.splitlines() if line.strip()]
    event_date, time_from = parse_date_time(text)
    if not event_date:
        return None

    date_line_index = next(
        (index for index, line in enumerate(lines) if DATE_TIME_RE.search(line)),
        -1,
    )
    venue = extract_venue(lines, date_line_index) if date_line_index >= 0 else None
    title = clean_text(html.unescape(post.get('title', {}).get('rendered', '')))
    title = TITLE_DATE_RE.sub('', title).strip(' -–—')
    if not title or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': post['link'],
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    posts = event_posts(session)
    records = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(get_response, session, post['link']): post
            for post in posts
        }
        for future in as_completed(futures):
            post = futures[future]
            try:
                record = parse_event(future.result().content, post)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped Alnwick Music Society post with incomplete event details',
                        event='crawler_item_skipped',
                        level='warning',
                        url=post.get('link'),
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Alnwick Music Society event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=post.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class AlnwickMusicSocietyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='alnwickmusicsociety_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
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
    AlnwickMusicSocietyCrawler().run()


if __name__ == '__main__':
    main()
