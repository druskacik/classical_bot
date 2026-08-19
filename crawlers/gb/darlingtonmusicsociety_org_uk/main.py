import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://darlingtonmusicsociety.org.uk/'
SOURCE = 'Darlington Music Society'
CURRENT_URL = f'{SOURCE_URL}Concerts/'
PAST_URL = f'{SOURCE_URL}Past-Concerts/'
VENUE = 'Central Hall at the Dolphin Centre'
CITY = 'Darlington'
CENTRAL_HALL_FIRST_DATE = '2012-10-14'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'
)
DATE_RE = re.compile(
    rf'^\s*(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|\s)\s*({MONTHS})\s*(?:-|\s)\s*(\d{{2}}|\d{{4}})\b'
    rf'|^\s*(\d{{1,2}})\s*[-/]\s*(\d{{1,2}})\s*[-/]\s*(\d{{2}}|\d{{4}})\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_heading(text):
    match = DATE_RE.match(text)
    if not match:
        return None

    if match.group(1):
        day, month, year = match.group(1), match.group(2), match.group(3)
        date_text = f'{day} {month} {year}'
        formats = ('%d %B %Y', '%d %b %Y', '%d %B %y', '%d %b %y')
    else:
        day, month, year = match.group(4), match.group(5), match.group(6)
        date_text = f'{day}-{month}-{year}'
        formats = ('%d-%m-%Y', '%d-%m-%y')

    if len(year) == 2:
        # The archive uses two-digit years. It is reverse chronological, and
        # values later than the current two-digit year belong to the 1900s.
        current_two_digit_year = datetime.now().year % 100
        year = str((2000 if int(year) <= current_two_digit_year else 1900) + int(year))
        date_text = (
            f'{day} {month} {year}' if match.group(1)
            else f'{day}-{month}-{year}'
        )

    event_date = None
    for date_format in formats:
        try:
            event_date = datetime.strptime(date_text, date_format).date().isoformat()
            break
        except ValueError:
            continue
    if not event_date:
        return None

    title = text[match.end():].strip(' \u2013\u2014-:,*')
    return event_date, title


def content_paragraphs(content):
    soup = BeautifulSoup(content, 'html.parser')
    return [clean_text(node) for node in soup.select('p')]


def parse_listing(content, url, *, time_from, earliest_date=None):
    records = []
    current = None
    archive_finished = False

    def finish_record():
        if not current or not current['title']:
            return
        if earliest_date and current['date'] < earliest_date:
            return
        description_parts = [
            part for part in current.pop('_parts')
            if part
            and not re.search(
                r'visit our tickets page|season tickets?|^all text copyright|^-{8,}$',
                part,
                re.IGNORECASE,
            )
        ]
        current['description'] = '\n'.join(description_parts) or None
        records.append(current.copy())

    for paragraph in content_paragraphs(content):
        heading = parse_heading(paragraph)
        if heading:
            finish_record()
            event_date, title = heading
            if earliest_date and event_date < earliest_date:
                archive_finished = True
                current = None
                continue
            if archive_finished:
                continue
            current = {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': VENUE,
                'city': CITY,
                'country_code': 'GB',
                '_parts': [],
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        elif current and paragraph:
            current['_parts'].append(paragraph)
    finish_record()
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    feeds = (
        (CURRENT_URL, '19:30', None),
        (PAST_URL, None, CENTRAL_HALL_FIRST_DATE),
    )
    for url, time_from, earliest_date in feeds:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(
                parse_listing(
                    response.content,
                    url,
                    time_from=time_from,
                    earliest_date=earliest_date,
                )
            )
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Darlington Music Society listing',
                event='crawler_feed_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class DarlingtonMusicSocietyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='darlingtonmusicsociety_org_uk',
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
        return get_concerts()


def main():
    DarlingtonMusicSocietyCrawler().run()


if __name__ == '__main__':
    main()
