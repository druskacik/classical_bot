import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chambermusicstl.org/'
SOURCE = 'Chamber Music Society of St. Louis'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concerts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The site sometimes publishes only an address, rather than a venue name.
# These are the venue/address pairs used by its own concert pages.
LOCATION_RULES = (
    ('560 trinity', '560 Music Center', 'University City'),
    ('first presbyterian', 'First Presbyterian Church of Kirkwood', 'Kirkwood'),
    ('1005 mccausland', 'Hi-Pointe Theatre', 'St. Louis'),
    ('hi-pointe theatre', 'Hi-Pointe Theatre', 'St. Louis'),
    ('1 university boulevard', 'Touhill Performing Arts Center', 'St. Louis'),
    ('3648 washington', 'Centene Center for the Arts', 'St. Louis'),
    ('chandler hill vineyards', 'Chandler Hill Vineyards', 'Defiance'),
)

DATE_RE = re.compile(
    r'(?P<date>(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4})'
    r'\s+Time:\s+(?P<time>\d{1,2}:\d{2}\s*[ap]m)'
    r'(?P<label>.*?)(?=(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),|$)',
    re.IGNORECASE | re.DOTALL,
)


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_location(value):
    normalized = value.casefold()
    for marker, venue, city in LOCATION_RULES:
        if marker in normalized:
            return venue, city
    return None


def parse_occurrences(value):
    occurrences = []
    for match in DATE_RE.finditer(value):
        # Virtual availability dates are recordings/streams rather than a
        # concrete live performance and are outside the project scope.
        if 'virtual' in match.group('label').casefold():
            continue
        try:
            event_date = datetime.strptime(match.group('date'), '%A, %b %d, %Y').date()
            event_time = datetime.strptime(
                match.group('time').replace(' ', '').upper(), '%I:%M%p'
            ).time()
        except ValueError:
            continue
        occurrences.append((event_date.isoformat(), event_time.strftime('%H:%M')))
    return occurrences


def parse_detail(post, response_text):
    soup = BeautifulSoup(response_text, 'html.parser')
    location_element = soup.select_one('.venue-title')
    location = parse_location(clean_text(location_element))
    if location is None:
        return []

    dates_element = soup.select_one('.concert-dates-content-wrapper')
    occurrences = parse_occurrences(clean_text(dates_element))
    if not occurrences:
        return []

    title = BeautifulSoup(
        html.unescape(post.get('title', {}).get('rendered', '')), 'html.parser'
    ).get_text(' ', strip=True)
    url = post.get('link', '').strip()
    if not title or not url:
        return []

    description_html = post.get('content', {}).get('rendered', '')
    description = clean_text(BeautifulSoup(description_html, 'html.parser')) or None
    venue, city = location
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'description': description,
        }
        for event_date, event_time in occurrences
    ]


class ChamberMusicStlOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicstl_org',
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
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def fetch_posts(self, session):
        posts = []
        page = 1
        while True:
            response = session.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    '_fields': 'link,title,content',
                },
                timeout=45,
            )
            response.raise_for_status()
            posts.extend(response.json())
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                return posts
            page += 1

    def scrape(self):
        session = make_session()
        try:
            posts = self.fetch_posts(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Chamber Music STL concert feed',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, post['link'], timeout=45): post
                for post in posts
                if post.get('link')
            }
            for future in as_completed(futures):
                post = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    records.extend(parse_detail(post, response.text))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Chamber Music STL concert detail',
                        event='crawler_detail_fetch_failed',
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


def main():
    ChamberMusicStlOrgCrawler().run()


if __name__ == '__main__':
    main()
