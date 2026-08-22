import calendar
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opus3artists.com/artists/isabelle-faust/'
SOURCE = 'Opus 3 Artists – Isabelle Faust'
API_URL = 'https://www.opus3artists.com/'
ARTIST_TAG_ID = 4853

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}

# Tour-date lines on this artist's first-party news archive identify locations by
# city but do not include country names on every line.
CITY_COUNTRIES = {
    'grafenegg': ('Grafenegg', 'AT'),
    'leipzig': ('Leipzig', 'DE'),
    'london': ('London', 'GB'),
    'paris': ('Paris', 'FR'),
    'san sebastian': ('San Sebastián', 'ES'),
    'santander': ('Santander', 'ES'),
}

DATE_LINE = re.compile(
    r'^(?P<month>[A-Z][a-z]{2,8})\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>20\d{2}))?\s+'
    r'(?P<location>.+)$'
)


def clean_text(value):
    text = BeautifulSoup(value or '', 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def closest_date(month, day, published, explicit_year=None):
    years = [explicit_year] if explicit_year else [published.year - 1, published.year, published.year + 1]
    candidates = []
    for year in years:
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda value: abs((value - published).days)).isoformat()


def parse_location(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return None

    location_prefix = parts[0].casefold()
    city_match = next(
        (
            value for key, value in CITY_COUNTRIES.items()
            if location_prefix == key or location_prefix.startswith(f'{key} ')
        ),
        None,
    )
    if city_match is None:
        return None

    city, country_code = city_match
    venue = parts[-1]
    venue = re.sub(r'\s*\([^)]*\)\s*$', '', venue).strip()
    if not venue:
        return None
    return venue, city, country_code


def parse_post(post):
    content_html = (post.get('content') or {}).get('rendered') or ''
    description = clean_text(content_html)
    title = clean_text((post.get('title') or {}).get('rendered'))
    url = str(post.get('link') or '').strip()
    try:
        published = date.fromisoformat(str(post.get('date') or '')[:10])
    except ValueError:
        return []
    if not title or not url or not description:
        return []

    records = []
    in_schedule = False
    for line in description.splitlines():
        line = line.strip(' \t–—-')
        if re.fullmatch(r'(?:tour|performance|concert)\s+dates?:', line, re.I):
            in_schedule = True
            continue
        if not in_schedule:
            continue

        match = DATE_LINE.match(line)
        if not match:
            continue
        month = MONTHS.get(match.group('month')[:3].lower())
        location = parse_location(match.group('location'))
        if not month or not location:
            continue
        event_date = closest_date(
            month,
            int(match.group('day')),
            published,
            int(match.group('year')) if match.group('year') else None,
        )
        if not event_date:
            continue

        venue, city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class Opus3ArtistsComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opus3artists_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            params = {
                'rest_route': '/wp/v2/posts',
                'tags': ARTIST_TAG_ID,
                'per_page': 50,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
            }
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                posts = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Opus 3 Artists news archive',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            if not isinstance(posts, list):
                raise ValueError('Opus 3 Artists API returned an unexpected payload')
            for post in posts:
                records.extend(parse_post(post))

            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda item: (item['date'], item['city'], item['venue'], item['url']),
        )


def main():
    Opus3ArtistsComCrawler().run()


if __name__ == '__main__':
    main()
