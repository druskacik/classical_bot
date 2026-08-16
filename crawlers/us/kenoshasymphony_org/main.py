import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kenoshasymphony.org/'
SOURCE = 'Kenosha Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
EVENT_CATEGORY_IDS = (9, 14)  # Events and Past Events
CITY = 'Kenosha'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})'
    r'\s*(?:[-–—]\s*)?(\d{1,2}(?::\d{2})?\s*[ap]m)?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None, None
    month_day, year, raw_time = match.groups()
    try:
        event_date = datetime.strptime(f'{month_day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None, None

    event_time = None
    if raw_time:
        for pattern in ('%I:%M %p', '%I %p'):
            try:
                event_time = datetime.strptime(raw_time.upper(), pattern).strftime('%H:%M')
                break
            except ValueError:
                pass
    return event_date, event_time


def extract_venue(description):
    lines = [line.strip(' -–—') for line in description.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        match = re.match(r'Location:\s*(.+)', line, re.IGNORECASE)
        if not match and re.fullmatch(r'Location:', line, re.IGNORECASE) and index + 1 < len(lines):
            location = lines[index + 1]
        elif match:
            location = match.group(1)
        else:
            continue
        venue = re.split(r'\s+\d{2,5}\s+', location, maxsplit=1)[0]
        venue = re.split(r'\s+Kenosha,?\s+WI\b', venue, maxsplit=1, flags=re.I)[0]
        return venue.strip(' ,.-') or None

    if re.search(r'\bSimmons Field\b', description, re.IGNORECASE):
        return 'Simmons Field'
    return None


def fetch_category_posts(session, category_id):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'categories': category_id,
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,content,excerpt',
            },
            timeout=45,
        )
        response.raise_for_status()
        posts.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return posts
        page += 1


def post_to_record(post):
    title = clean_text(post.get('title', {}).get('rendered'))
    url = post.get('link', '').strip()
    event_date, event_time = parse_date_time(post.get('excerpt', {}).get('rendered'))
    description = clean_text(post.get('content', {}).get('rendered'))
    venue = extract_venue(description)

    if not title or not url.startswith(('http://', 'https://')) or not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    posts_by_id = {}
    for category_id in EVENT_CATEGORY_IDS:
        for post in fetch_category_posts(session, category_id):
            posts_by_id[post['id']] = post

    records = []
    for post in posts_by_id.values():
        record = post_to_record(post)
        if record:
            records.append(record)
        else:
            log_message(
                'Skipping event post without a valid date or venue',
                event='crawler_record_skipped',
                level='warning',
                url=post.get('link'),
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class KenoshaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kenoshasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
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
        return scrape_concerts()


def main():
    KenoshaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
