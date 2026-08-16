import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://clefworks.org/'
SOURCE = 'ClefWorks'
POSTS_API = f'{SOURCE_URL}wp-json/wp/v2/posts'
CATEGORY_IDS = '1,5,11'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTH_PATTERN = (
    r'January|February|March|April|May|June|July|August|September|'
    r'October|November|December'
)
DATE_PATTERN = re.compile(
    rf'(?i)(?:(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    rf'({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?'
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def fetch_posts(session):
    """Fetch every post in the stable, first-party concert-related categories."""
    posts = []
    page = 1
    while True:
        response = get_response(
            session,
            POSTS_API,
            params={
                'categories': CATEGORY_IDS,
                'per_page': 100,
                'page': page,
                '_fields': 'id,date,link,title,content,categories',
            },
        )
        posts.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return posts
        page += 1


def extract_date(text, published_at):
    match = DATE_PATTERN.search(text)
    if not match:
        return None

    _, month, day, year = match.groups()
    published = datetime.fromisoformat(published_at).date()
    if year is None:
        # Archive reports often describe a concert earlier in the publication
        # year; upcoming notices likewise normally mean that same year.
        year = str(published.year)
    try:
        parsed = date_parser.parse(f'{month} {day}, {year}', fuzzy=False)
        return date(parsed.year, parsed.month, parsed.day).isoformat()
    except (ValueError, OverflowError):
        return None


def extract_time(text):
    match = re.search(r'(?i)\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?\b', text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def extract_venue(text):
    venue_patterns = (
        (r'(?i)\bThe Warehouse at Alley Station\b', 'The Warehouse at Alley Station'),
        (r'(?i)\b(?:The )?Capri (?:Theatre|Theater)\b', 'The Capri Theatre'),
        (r'(?i)\bMontgomery Museum of Fine Arts\b|\bMMFA\b', 'Montgomery Museum of Fine Arts'),
        (r'(?i)\bAlabama Shakespeare Festival\b', 'Alabama Shakespeare Festival'),
        (r'(?i)\bBrock Recital Hall\b', 'Brock Recital Hall'),
        (r'(?i)\bCity Hall\b', 'City Hall'),
    )
    for pattern, venue in venue_patterns:
        if re.search(pattern, text):
            return venue
    return None


def make_record(post):
    title = clean_text(post.get('title', {}).get('rendered'))
    description = clean_text(post.get('content', {}).get('rendered'))
    text = '\n'.join(value for value in (title, description) if value)
    event_date = extract_date(text, post['date'])
    venue = extract_venue(text)
    url = post.get('link')
    if not title or not event_date or not venue or not url:
        return None

    city = 'Birmingham' if re.search(r'(?i)\bBirmingham,?\s+Alabama\b', text) else 'Montgomery'
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': extract_time(text),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    try:
        posts = fetch_posts(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch ClefWorks posts',
            event='crawler_fetch_failed',
            level='error',
            url=POSTS_API,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    for post in posts:
        try:
            record = make_record(post)
        except (KeyError, TypeError, ValueError) as error:
            log_message(
                'Failed to parse ClefWorks post',
                event='crawler_item_failed',
                level='warning',
                url=post.get('link', POSTS_API),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class ClefworksOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='clefworks_org',
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
        return get_concerts()


def main():
    ClefworksOrgCrawler().run()


if __name__ == '__main__':
    main()
