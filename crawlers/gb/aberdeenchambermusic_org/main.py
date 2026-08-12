import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.aberdeenchambermusic.org/'
PROGRAMME_URL = f'{SOURCE_URL}programme/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
SOURCE = 'Aberdeen Chamber Music Concerts'
CITY = 'Aberdeen'
VENUE = 'Fountainhall at the Cross Church'
REVIEW_CATEGORY_ID = 5

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(\d{1,2})(?:st|nd|rd|th)?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(20\d{2})\b',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def api_get(session, path, params):
    url = f'{API_URL}/{path}'
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def record(title, event_date, url, description, venue=VENUE):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def current_programme(session):
    response = api_get(
        session,
        'pages',
        {'slug': 'programme', '_fields': 'link,content'},
    )
    pages = response.json()
    if not pages:
        return []

    page = pages[0]
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    records = []
    headings = soup.find_all('h3')
    for index, heading in enumerate(headings):
        event_date = parse_date(heading)
        if not event_date:
            continue

        title_node = next(
            (node for node in headings[index + 1:] if clean_text(node)),
            None,
        )
        if title_node is None:
            continue
        title = clean_text(title_node)
        details = []
        event_url = page['link']
        for node in title_node.next_elements:
            if isinstance(node, Tag) and node.name == 'hr':
                break
            if isinstance(node, Tag) and node.name == 'a' and node.get('href'):
                if 'BOOK TICKETS' in clean_text(node).upper():
                    event_url = node['href']
            if isinstance(node, Tag) and node.name in {'p', 'tr'}:
                text = clean_text(node)
                if text and text not in details:
                    details.append(text)
        records.append(record(title, event_date, event_url, '\n'.join(details)))
    return records


def review_title_and_date(raw_title):
    title = clean_text(raw_title)
    event_date = parse_date(title)
    if not event_date:
        return None, None
    title = DATE_RE.sub('', title, count=1)
    title = re.sub(r'\s*[-–—:]?\s*Review\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*[-–—:]\s*$', '', title).strip()
    return title or None, event_date


def review_venue(description):
    opening = description[:800].lower().replace('’', "'")
    if "queen's cross church" in opening:
        return "Queen's Cross Church"
    if 'fountainhall at the cross' in opening:
        return VENUE
    return VENUE


def past_reviews(session):
    records = []
    page = 1
    while True:
        response = api_get(
            session,
            'posts',
            {
                'categories': REVIEW_CATEGORY_ID,
                'per_page': 100,
                'page': page,
                '_fields': 'link,title,content,categories',
            },
        )
        posts = response.json()
        for post in posts:
            if REVIEW_CATEGORY_ID not in post.get('categories', []):
                continue
            description = clean_text(BeautifulSoup(post['content']['rendered'], 'html.parser'))
            title, event_date = review_title_and_date(post['title']['rendered'])
            if not event_date:
                event_date = parse_date(description[:1200])
                title = clean_text(post['title']['rendered'])
                title = re.sub(r'\s*[-–—:]?\s*Review\s*$', '', title, flags=re.IGNORECASE)
                title = re.sub(r'^Review\s*[-–—:]?\s*', '', title, flags=re.IGNORECASE)
            if not title or not event_date:
                continue
            records.append(
                record(
                    title,
                    event_date,
                    post['link'],
                    description,
                    review_venue(description),
                )
            )
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return records


class AberdeenChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aberdeenchambermusic_org',
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
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for scraper, feed_name in (
            (current_programme, 'programme'),
            (past_reviews, 'reviews'),
        ):
            try:
                records.extend(scraper(session))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Aberdeen Chamber Music feed',
                    event='crawler_feed_failed',
                    level='warning',
                    url=PROGRAMME_URL if feed_name == 'programme' else API_URL,
                    feed=feed_name,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda item: (item['date'], item['title'], item['url']),
        )


def main():
    AberdeenChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
