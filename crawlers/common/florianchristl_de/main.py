import html
import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://florianchristl.de/'
SOURCE = 'Florian Christl'
CONCERTS_URL = urljoin(SOURCE_URL, 'pages/concerts')
DIARY_API_URL = 'https://diary.florianchristl.de/wp-json/wp/v2/posts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9,de;q=0.7',
}

# The diary uses UK and, once, the US state abbreviation TX in its country slot.
COUNTRY_CODES = {'UK': 'GB', 'TX': 'US'}
INVALID_VENUES = {
    'solo piano',
    'tickets soon available',
    'ticket soon available',
    'tickets available soon',
}


def clean_text(value):
    if not value:
        return ''
    raw = html.unescape(str(value))
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw.strip()
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalize_country_code(value):
    code = clean_text(value).upper()
    code = COUNTRY_CODES.get(code, code)
    return code if re.fullmatch(r'[A-Z]{2}', code) else ''


def parse_diary_post(post):
    raw_title = clean_text((post.get('title') or {}).get('rendered'))
    match = re.fullmatch(r'(.+?)\s*\(([A-Za-z]{2,3})\)\s*\|\s*(.+)', raw_title)
    if not match:
        return None

    city = clean_text(match.group(1))
    country_code = normalize_country_code(match.group(2))
    venue = clean_text(match.group(3))
    description = clean_text((post.get('content') or {}).get('rendered')) or None
    url = clean_text(post.get('link'))

    # This is the date displayed for the event in the diary listing. Some body
    # copy contains conflicting templated dates, so the structured value wins.
    try:
        event_date = datetime.fromisoformat(clean_text(post.get('date'))).date().isoformat()
    except ValueError:
        event_date = ''

    if not all((city, country_code, venue, event_date, url)):
        return None

    return {
        'title': f'Florian Christl — {venue}',
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def upcoming_date(value, today=None):
    today = today or date.today()
    for pattern in ('%b %d', '%B %d'):
        try:
            parsed = datetime.strptime(clean_text(value), pattern)
            candidate = date(today.year, parsed.month, parsed.day)
            if candidate < today:
                candidate = date(today.year + 1, parsed.month, parsed.day)
            return candidate.isoformat()
        except ValueError:
            pass
    return ''


def parse_upcoming_item(item, today=None):
    location = clean_text(item.select_one('.event__location'))
    location_match = re.fullmatch(r'(.+?)\s*\(([A-Za-z]{2,3})\)', location)
    if not location_match:
        return None

    city = clean_text(location_match.group(1))
    country_code = normalize_country_code(location_match.group(2))
    venue = clean_text(item.select_one('.event__venue'))
    if not venue or venue.casefold() in INVALID_VENUES or venue.casefold() == city.casefold():
        return None

    event_date = upcoming_date(clean_text(item.select_one('.event__date')), today=today)
    link = item.select_one('.event__link a[href]')
    url = urljoin(CONCERTS_URL, link['href']) if link else CONCERTS_URL
    if not all((event_date, city, country_code, venue, url)):
        return None

    return {
        'title': f'Florian Christl — {venue}',
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FlorianChristlDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='florianchristl_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'venue', 'city', 'country_code'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            concerts_response = session.get(CONCERTS_URL, timeout=45)
            concerts_response.raise_for_status()

            diary_response = session.get(
                DIARY_API_URL,
                params={'per_page': 100, 'page': 1},
                timeout=45,
            )
            diary_response.raise_for_status()
            diary_posts = diary_response.json()
            total_pages = int(diary_response.headers.get('X-WP-TotalPages', '1'))
            for page in range(2, total_pages + 1):
                response = session.get(
                    DIARY_API_URL,
                    params={'per_page': 100, 'page': page},
                    timeout=45,
                )
                response.raise_for_status()
                diary_posts.extend(response.json())
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Florian Christl concerts',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        skipped_count = 0
        for post in diary_posts:
            record = parse_diary_post(post)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        concerts_soup = BeautifulSoup(concerts_response.text, 'html.parser')
        for item in concerts_soup.select('.tour-events .event'):
            record = parse_upcoming_item(item)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        if skipped_count:
            log_message(
                'Skipped incomplete Florian Christl concert entries',
                event='crawler_items_skipped',
                level='warning',
                url=SOURCE_URL,
                record_count=skipped_count,
                error_type='IncompleteEventData',
                error_message='Required date, venue, city, country, or URL was unavailable',
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['city'], record['venue']
            ),
        )


def main():
    FlorianChristlDeCrawler().run()


if __name__ == '__main__':
    main()
