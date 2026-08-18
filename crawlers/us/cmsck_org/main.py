import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cmsck.org/'
SOURCE = 'Chamber Music Society of Central Kentucky'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}):([0-5]\d)\s*([AP])\.?\s*M\.?', re.IGNORECASE)


def clean_text(value):
    text = BeautifulSoup(html.unescape(value or ''), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'P':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_venue(content_html):
    soup = BeautifulSoup(content_html or '', 'html.parser')
    for element in soup.select('strong, b'):
        candidate = clean_text(str(element))
        if not candidate or DATE_RE.search(candidate) or TIME_RE.search(candidate):
            continue
        if re.search(r'new date|tickets?|free|sold out', candidate, re.IGNORECASE):
            continue
        candidate = re.sub(r',\s*\d+\s+.+$', '', candidate).strip()
        if not candidate:
            continue
        return candidate
    return None


def parse_post(post):
    title = clean_text(post.get('title', {}).get('rendered'))
    url = post.get('link')
    content_html = post.get('content', {}).get('rendered', '')
    description = clean_text(content_html)
    event_date = parse_date(description)
    venue = parse_venue(content_html)
    if not title or not url or not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(description),
        'venue': venue,
        'city': 'Lexington',
        'description': description or None,
    }


class CmsckOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cmsck_org',
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
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        page = 1
        records = []

        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        '_fields': 'id,link,title,content,categories',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                posts = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch CMSCK WordPress posts',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for post in posts:
                record = parse_post(post)
                if record:
                    records.append(record)

            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CmsckOrgCrawler().run()


if __name__ == '__main__':
    main()
