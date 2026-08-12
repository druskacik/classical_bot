import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://swindonrecitalseries.org/'
SOURCE = 'Swindon Recital Series'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The series publishes performances at these Swindon venues. Restricting the
# default city to known local venues prevents a future touring post from being
# assigned the organisation's home city.
VENUES = {
    'wyvern theatre': 'Wyvern Theatre',
    'arts centre': 'Swindon Arts Centre',
    'swindon arts centre': 'Swindon Arts Centre',
    'christ church': 'Christ Church',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    normalized = re.sub(r'(?<=\d)(?:st|nd|rd|th)\b', '', value, flags=re.IGNORECASE)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    for pattern in ('%A %d %B %Y', '%d %B %Y'):
        try:
            return datetime.strptime(normalized, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_location(value):
    match = re.match(
        r'\s*(.+?)\s*@\s*(\d{1,2})(?:[:.]([0-5]\d))?\s*(am|pm)\s*$',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None

    raw_venue = re.sub(r'\s+', ' ', match.group(1)).strip()
    venue = VENUES.get(raw_venue.lower())
    if not venue:
        return None

    hour = int(match.group(2))
    if not 1 <= hour <= 12:
        return None
    minute = int(match.group(3) or 0)
    if match.group(4).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(4).lower() == 'am' and hour == 12:
        hour = 0
    return venue, 'Swindon', f'{hour:02d}:{minute:02d}'


def description_from(soup):
    parts = []
    for element in soup.select('h3, h4, p, li'):
        text = clean_text(element)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_post(post):
    title = clean_text(post.get('title', {}).get('rendered'))
    url = post.get('link', '').strip()
    soup = BeautifulSoup(post.get('content', {}).get('rendered', ''), 'html.parser')
    headings = [clean_text(element) for element in soup.select('h2')]
    event_date = next((parse_date(value) for value in headings if parse_date(value)), None)
    location = next((parse_location(value) for value in headings if parse_location(value)), None)
    if not title or not url or not event_date or not location:
        return None

    venue, city, time_from = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description_from(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SwindonRecitalSeriesOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='swindonrecitalseries_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        'status': 'publish',
                        '_fields': 'id,link,title,content',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                posts = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Swindon Recital Series posts',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            for post in posts:
                record = parse_post(post)
                if record:
                    records.append(record)
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SwindonRecitalSeriesOrgCrawler().run()


if __name__ == '__main__':
    main()
