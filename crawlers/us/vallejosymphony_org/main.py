import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://vallejosymphony.org/'
SOURCE = 'Vallejo Symphony'
VENUE = 'Hogan Auditorium'
CITY = 'Vallejo'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>[AP]M)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    date_text = f"{match.group('month')} {match.group('day')} {match.group('year')}"
    for date_format in ('%B %d %Y', '%b %d %Y'):
        try:
            return datetime.strptime(date_text, date_format).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group('hour')}:{match.group('minute') or '00'} {match.group('period')}",
            '%I:%M %p',
        ).strftime('%H:%M')
    except ValueError:
        return None


def parse_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('main h1, main h2, main h3')
    records = []

    for index, heading in enumerate(headings):
        if heading.name != 'h1':
            continue

        title = clean_text(heading)
        detail_lines = []
        date_line = None
        for following in headings[index + 1:]:
            if following.name == 'h1':
                break
            text = clean_text(following)
            if not text:
                continue
            if DATE_RE.search(text):
                date_line = text
                break
            detail_lines.append(text)

        event_date = parse_date(date_line)
        if not title or not event_date or not date_line:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': SOURCE_URL,
            'time_from': parse_time(date_line),
            'venue': VENUE,
            'city': CITY,
            'country_code': COUNTRY_CODE,
            'description': '\n'.join(detail_lines) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return sorted(records, key=lambda record: (record['date'], record['title']))


class VallejoSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='vallejosymphony_org',
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
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        records = parse_page(response.text)
        if not records:
            log_message(
                'No concert records found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return records


def main():
    VallejoSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
