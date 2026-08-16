import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'http://www.ncchamberorchestra.org/'
SOURCE = 'North Carolina Chamber Orchestra'
VENUE = 'Well-Spring'
CITY = 'Greensboro'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

FULL_DATE_RE = re.compile(
    r'\b([A-Z][a-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d{2})\b'
)
SEASON_RE = re.compile(r'\b(20\d{2})\s*[-–]\s*(\d{2})\b')
SEASON_DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Z][a-z]+\.?)\s+(\d{1,2})(?:st|nd|rd|th)?\b'
    r'[^@]*@\s*(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\b', re.IGNORECASE)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value, year=None):
    text = re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', clean_text(value), flags=re.I)
    text = text.replace('.', '')
    if year is not None and not re.search(r'\b20\d{2}\b', text):
        text = f'{text} {year}'
    for pattern in ('%b %d, %Y', '%b %d %Y', '%B %d, %Y', '%B %d %Y'):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    text = clean_text(value).replace('.', '').upper()
    text = re.sub(r'(?<=\d)(AM|PM)$', r' \1', text)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(text, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def make_record(title, event_date, url, time_from, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_card(card):
    title_link = card.select_one('.post_title a[href]')
    description = clean_text(card.select_one('.post_descr'))
    title = clean_text(title_link)
    if not title_link or not title or not description:
        return []

    url = urljoin(SOURCE_URL, title_link.get('href', ''))
    season_match = SEASON_RE.search(title)
    if season_match:
        start_year = int(season_match.group(1))
        end_year = (start_year // 100) * 100 + int(season_match.group(2))
        records = []
        for index, match in enumerate(SEASON_DATE_RE.finditer(description), start=1):
            month, day, time_text = match.groups()
            normalized_month = month.replace('.', '')
            month_number = None
            for pattern in ('%b', '%B'):
                try:
                    month_number = datetime.strptime(normalized_month, pattern).month
                    break
                except ValueError:
                    continue
            if month_number is None:
                continue
            year = start_year if month_number >= 7 else end_year
            event_date = parse_date(f'{month} {day}', year)
            time_from = parse_time(time_text)
            if event_date and time_from:
                records.append(make_record(
                    f'{title} – Concert {index}', event_date, url, time_from, description
                ))
        return records

    date_match = FULL_DATE_RE.search(description)
    time_match = TIME_RE.search(description)
    event_date = parse_date(date_match.group(1)) if date_match else None
    time_from = parse_time(time_match.group(1)) if time_match else None
    if not event_date:
        return []
    return [make_record(title, event_date, url, time_from, description)]


class NcChamberOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ncchamberorchestra_org',
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
        try:
            response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch North Carolina Chamber Orchestra homepage',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.content, 'html.parser')
        records = []
        for card in soup.select('.isotope_item .post_item'):
            records.extend(parse_card(card))

        if not records:
            log_message(
                'No dated concert cards found on North Carolina Chamber Orchestra homepage',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    NcChamberOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
