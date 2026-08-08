import html
from datetime import datetime
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chhayanaut.org/'
SOURCE = 'Chhayanaut'
PROGRAMS_URL = f'{SOURCE_URL}programs/0'
API_URL = f'{SOURCE_URL}ajax/programs'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'bn-BD,bn;q=0.9,en;q=0.7',
}

BENGALI_DIGITS = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')
MONTHS = {
    'জানুয়ারি': 1,
    'ফেব্রুয়ারি': 2,
    'মার্চ': 3,
    'এপ্রিল': 4,
    'মে': 5,
    'জুন': 6,
    'জুলাই': 7,
    'আগস্ট': 8,
    'অগাস্ট': 8,
    'সেপ্টেম্বর': 9,
    'অক্টোবর': 10,
    'নভেম্বর': 11,
    'ডিসেম্বর': 12,
}


def clean_html(value):
    if not value:
        return None
    # The API HTML-escapes the already marked-up description.
    decoded = html.unescape(value)
    text = BeautifulSoup(decoded, 'html.parser').get_text('\n', strip=True)
    text = '\n'.join(line.strip() for line in text.splitlines() if line.strip())
    return text or None


def parse_date(value):
    normalized = str(value or '').translate(BENGALI_DIGITS)
    matches = re.findall(r'(\d{1,2})\s+([^\s,]+)\s+(20\d{2})', normalized)
    for day, month_name, year in reversed(matches):
        month = MONTHS.get(month_name)
        if not month:
            continue
        try:
            return datetime(int(year), month, int(day)).date().isoformat()
        except ValueError:
            continue
    return None


def parse_record(item):
    title = str(item.get('name') or '').strip()
    venue = str(item.get('program_venue') or '').strip()
    item_id = item.get('id')

    # Virtual programmes have no defensible physical city. They remain available
    # on the source site, but cannot form a valid concert location record.
    if not title or not venue or not item_id or 'অনলাইন' in venue or 'ইউটিউব' in venue:
        return None

    event_date = parse_date(item.get('program_start_date'))
    if not event_date:
        return None

    city = 'Rajshahi' if 'রাজশাহী' in venue else 'Dhaka'
    return {
        'title': title,
        'date': event_date,
        'url': f'{SOURCE_URL}program/{item_id}',
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': 'BD',
        'description': clean_html(item.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ChhayanautOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chhayanaut_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BD',
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
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            page_response = session.get(PROGRAMS_URL, timeout=45)
            page_response.raise_for_status()
            soup = BeautifulSoup(page_response.text, 'html.parser')
            csrf = soup.select_one('meta[name="csrf-token"]')
            if csrf is None or not csrf.get('content'):
                raise ValueError('Programs page did not expose a CSRF token')

            response = session.post(
                API_URL,
                data={
                    'filter_program_venue': '',
                    'filter_program_category_id': '',
                    'filter_program_year': '',
                },
                headers={
                    'X-CSRF-TOKEN': csrf['content'],
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': PROGRAMS_URL,
                },
                timeout=60,
            )
            response.raise_for_status()
            items = response.json().get('data', [])
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Chhayanaut programmes',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = [record for item in items if (record := parse_record(item))]
        return sorted(
            records,
            key=lambda record: (record['date'], record['title'], record['venue']),
        )


def main():
    ChhayanautOrgCrawler().run()


if __name__ == '__main__':
    main()
