import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://topekasymphony.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'Topeka Symphony Orchestra'
COUNTRY_CODE = 'US'
CITY = 'Topeka'
DEFAULT_VENUE = 'White Concert Hall'
CONCERTS_CATEGORY_ID = 11

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'\b[A-Z][a-z]+ \d{1,2}, \d{4}\b')
TICKET_DATETIME_RE = re.compile(
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)\s+'
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def description_from_content(content):
    soup = BeautifulSoup(content or '', 'html.parser')
    parts = []
    for node in soup.select('p, h2, h3, h4'):
        if node.find_parent(class_='wp-block-buttons'):
            continue
        text = clean_text(node.get_text('\n', strip=True))
        if not text or parse_date(text) or text.lower() in {'get tickets', 'purchase tickets'}:
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def ticket_details(session, ticket_url):
    if not ticket_url or 'app.arts-people.com' not in ticket_url:
        return None, None, None
    try:
        response = session.get(ticket_url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Ticket detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=ticket_url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None, None, None

    soup = BeautifulSoup(response.text, 'html.parser')
    headings = [clean_text(node.get_text(' ', strip=True)) for node in soup.select('#show_text h3')]
    date_value = time_value = venue = None
    for heading in headings:
        match = TICKET_DATETIME_RE.search(heading)
        if match:
            date_value = parse_date(match.group('date'))
            time_value = parse_time(match.group('time'))
        elif heading:
            venue = heading
    return date_value, time_value, venue


def post_to_record(post, session):
    content = post.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(BeautifulSoup(post.get('title', {}).get('rendered', ''), 'html.parser').get_text())
    url = clean_text(post.get('link'))
    event_date = parse_date(soup.get_text(' ', strip=True))
    ticket = soup.select_one('a[href*="app.arts-people.com"]')
    ticket_url = ticket.get('href') if ticket else None
    ticket_date, time_from, venue = ticket_details(session, ticket_url)

    if ticket_date:
        event_date = ticket_date
    venue = clean_text(venue) or DEFAULT_VENUE
    if not title or not event_date or not url or not venue:
        log_message(
            'Skipping incomplete concert',
            event='crawler_record_skipped',
            level='warning',
            url=url or SOURCE_URL,
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': description_from_content(content),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'categories': CONCERTS_CATEGORY_ID,
                'per_page': 100,
                'page': page,
                '_fields': 'content,link,title',
            },
            timeout=45,
        )
        response.raise_for_status()
        posts = response.json()
        for post in posts:
            record = post_to_record(post, session)
            if record:
                records.append(record)

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TopekaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='topekasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
        return scrape_concerts()


def main():
    TopekaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
