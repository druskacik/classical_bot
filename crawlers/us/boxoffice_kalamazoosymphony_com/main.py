import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kalamazoosymphony.com/'
SOURCE = 'Kalamazoo Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concerts'
CITY = 'Kalamazoo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

OCCURRENCE_RE = re.compile(
    r'^(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|'
    r'Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\s+\d{1,2},\s+\d{4}\s*\|\s*\d{1,2}(?::\d{2})?\s*[ap]\.?(?:m\.?)?$',
    re.IGNORECASE,
)


def clean_text(value):
    text = html.unescape(str(value or '')).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def content_lines(rendered_html):
    soup = BeautifulSoup(rendered_html or '', 'html.parser')
    for node in soup.select('style, script, noscript'):
        node.decompose()
    return [
        line for line in (clean_text(value) for value in soup.get_text('\n').splitlines())
        if line
    ]


def parse_occurrence(value):
    normalized = clean_text(value).replace('.', '')
    normalized = re.sub(r'^Sept\b', 'Sep', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'\s*\|\s*', ' ', normalized)
    for pattern in ('%B %d, %Y %I:%M %p', '%B %d, %Y %I %p',
                    '%b %d, %Y %I:%M %p', '%b %d, %Y %I %p'):
        try:
            parsed = datetime.strptime(normalized, pattern)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            pass
    return None


def extract_description(lines):
    try:
        start = lines.index('Program and Details') + 1
    except ValueError:
        return None

    try:
        end = lines.index('Artists', start)
    except ValueError:
        end = len(lines)

    description = '\n'.join(lines[start:end]).strip()
    return description or None


def extract_occurrences(lines):
    ticket_indexes = [index for index, line in enumerate(lines) if line.upper() == 'CHOOSE TICKETS']
    start = ticket_indexes[-1] + 1 if ticket_indexes else 0
    occurrences = []
    for index in range(start, len(lines)):
        if not OCCURRENCE_RE.fullmatch(lines[index]):
            continue
        parsed = parse_occurrence(lines[index])
        if not parsed:
            continue

        venue = ''
        for candidate in lines[index + 1:index + 4]:
            if candidate.lower().startswith(('get tickets', 'ticketing information')):
                break
            if candidate not in {'Monday', 'Tuesday', 'Wednesday', 'Thursday',
                                  'Friday', 'Saturday', 'Sunday'}:
                venue = candidate
                break
        if venue:
            occurrences.append((*parsed, venue))
    return occurrences


def fetch_posts(session):
    response = session.get(
        API_URL,
        params={
            'per_page': 100,
            '_fields': 'link,title,content',
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    posts = fetch_posts(session)

    records = []
    for post in posts:
        title = clean_text(post.get('title', {}).get('rendered'))
        url = clean_text(post.get('link'))
        lines = content_lines(post.get('content', {}).get('rendered'))
        description = extract_description(lines)
        if not title or not url:
            continue

        occurrences = extract_occurrences(lines)
        if not occurrences:
            log_message(
                'Concert has no parseable ticket occurrence',
                event='crawler_event_skipped',
                level='warning',
                url=url,
                error_type='MissingOccurrence',
            )
            continue

        for event_date, time_from, venue in occurrences:
            records.append({
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
            })

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BoxofficeKalamazooSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='boxoffice_kalamazoosymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
    BoxofficeKalamazooSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
