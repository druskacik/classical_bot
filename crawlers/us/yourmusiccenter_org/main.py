import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://yourmusiccenter.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'The Music Center of South Central Michigan'
DEFAULT_CITY = 'Battle Creek'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+'
    r'(?P<year>20\d{2})(?:,?\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?))?',
    re.IGNORECASE,
)
CITY_STATE_RE = re.compile(r'^([^,\n]+),\s*(?:MI|Michigan)(?:\s+\d{5})?$', re.IGNORECASE)
ADDRESS_RE = re.compile(r'^\d+\s+\S+')
KNOWN_VENUES = ('WK Kellogg Auditorium', 'W.K. Kellogg Auditorium')


def clean_lines(markup):
    soup = BeautifulSoup(markup or '', 'html.parser')
    lines = []
    for value in soup.get_text('\n', strip=True).splitlines():
        value = re.sub(r'\s+', ' ', html.unescape(value).replace('\xa0', ' ')).strip()
        if value:
            lines.append(value)
    return lines


def parse_date(match):
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {match.group('year')}",
            '%B %d %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    if not value:
        return None
    normalized = value.replace('.', '').replace(' ', '').upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def venue_and_city(value):
    city = DEFAULT_CITY
    if re.search(r'\bAlbion\b', value, re.IGNORECASE):
        city = 'Albion'
    elif re.search(r'\bBattle Creek\b', value, re.IGNORECASE):
        city = 'Battle Creek'

    for known_venue in KNOWN_VENUES:
        if known_venue.lower() in value.lower():
            return known_venue, city

    parts = [part.strip() for part in value.split(',') if part.strip()]
    clean_parts = []
    for part in parts:
        if ADDRESS_RE.match(part) or part.lower() in {'battle creek', 'albion'}:
            break
        clean_parts.append(part)
    venue = ', '.join(clean_parts) or value
    venue = re.sub(r',?\s+located\s+in\s+the\s+.*$', '', venue, flags=re.IGNORECASE).strip()
    return venue, city


def record_from_post(post):
    lines = clean_lines(post.get('content', {}).get('rendered'))
    title = BeautifulSoup(post.get('title', {}).get('rendered', ''), 'html.parser').get_text(' ', strip=True)
    url = post.get('link', '')

    for index, line in enumerate(lines):
        match = DATE_RE.match(line)
        if not match:
            continue
        event_date = parse_date(match)
        if not event_date or index + 1 >= len(lines):
            continue

        venue_line = lines[index + 1]
        if (
            not venue_line
            or ADDRESS_RE.match(venue_line)
            or len(venue_line) > 120
            or venue_line.rstrip(':').lower() in {'time', 'date', 'location', 'venue'}
        ):
            continue

        venue, city = venue_and_city(venue_line)
        for nearby in lines[index + 2:index + 5]:
            city_match = CITY_STATE_RE.match(nearby)
            if city_match:
                city = city_match.group(1).strip()
                break

        if not title or not url:
            return None
        return {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(match.group('time')),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': '\n'.join(lines) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
    return None


def fetch_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
                '_fields': 'link,title,content',
            },
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return posts


class YourMusicCenterOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='yourmusiccenter_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        posts = fetch_posts(session)
        records = [record for post in posts if (record := record_from_post(post))]
        if not records:
            log_message(
                'No concrete event records found',
                event='crawler_empty_listing',
                level='warning',
                url=API_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


def main():
    YourMusicCenterOrgCrawler().run()


if __name__ == '__main__':
    main()
