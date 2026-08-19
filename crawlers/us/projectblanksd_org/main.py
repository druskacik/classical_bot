import re
from datetime import date, timedelta
from urllib.parse import urldefrag, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.projectblanksd.org/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'archive')
SOURCE = 'Project [BLANK]'
CITY = 'San Diego'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number for number, name in enumerate(
        ('jan', 'feb', 'mar', 'apr', 'may', 'jun',
         'jul', 'aug', 'sep', 'oct', 'nov', 'dec'),
        start=1,
    )
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def valid_date(year, month, day):
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def inclusive_dates(start, end, maximum_days=7):
    if not start or not end or end < start or (end - start).days > maximum_days:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def archive_dates(text, start_year, end_year):
    # Some entries put spaces around separators, so retain the date-like prefix.
    match = re.match(
        r'^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?'
        r'\s*(?:(-|–|&|to)\s*(?:(\d{1,2})\.)?(\d{1,2})(?:\.(\d{2,4}))?)?',
        text,
        re.IGNORECASE,
    )
    if not match:
        return []

    month1, day1, year1, separator, month2, day2, year2 = match.groups()

    def resolved_year(month, explicit):
        if explicit:
            value = int(explicit)
            return 2000 + value if value < 100 else value
        return start_year if int(month) >= 7 else end_year

    first = valid_date(resolved_year(month1, year1), month1, day1)
    if not separator:
        return [first] if first else []
    month2 = month2 or month1
    second = valid_date(resolved_year(month2, year2), month2, day2)
    if separator == '&':
        return [value for value in (first, second) if value]
    return inclusive_dates(first, second)


def current_dates(text, fallback_year=None):
    numeric = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b', text)
    if numeric:
        month, day, year = numeric.groups()
        year = int(year)
        value = valid_date(2000 + year if year < 100 else year, month, day)
        return [value] if value else []

    named = re.search(
        r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+'
        r'(\d{1,2})(?:st|nd|rd|th)?(?:\s*[–-]\s*(\d{1,2}))?'
        r'(?:,?\s*(20\d{2}))?',
        text,
        re.IGNORECASE,
    )
    if not named:
        return []
    month_name, first_day, last_day, year = named.groups()
    year = int(year or fallback_year or 0)
    if not year:
        return []
    month = MONTHS[month_name[:3].lower()]
    first = valid_date(year, month, first_day)
    if not last_day:
        return [first] if first else []
    return inclusive_dates(first, valid_date(year, month, last_day))


def parse_time(text):
    time_range = re.search(
        r'\b(\d{1,2}):(\d{2})\s*[–-]\s*\d{1,2}:\d{2}\s*(am|pm)\b',
        text,
        re.IGNORECASE,
    )
    if time_range:
        hour, minute, meridiem = time_range.groups()
        hour = int(hour) % 12 + (12 if meridiem.lower() == 'pm' else 0)
        return f'{hour:02d}:{minute}'
    match = re.search(r'\b(\d{1,2}):(\d{2})\s*(am|pm)\b', text, re.IGNORECASE)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'pm' else 0)
    return f'{hour:02d}:{minute}'


def parse_venue(text):
    match = re.search(r'(?:→| at )\s*([^\n]+)', text, re.IGNORECASE)
    if match:
        return match.group(1).strip(' .')
    # Archive entries consistently end with the venue after their final comma.
    summary = re.sub(r'\bInfo\s*$', '', text, flags=re.IGNORECASE).strip()
    if ',' in summary:
        candidate = summary.rsplit(',', 1)[-1].strip().strip('.')
        if 2 < len(candidate) < 100:
            return candidate
    return None


def detail_text(session, cache, url):
    page_url, fragment = urldefrag(url)
    try:
        if page_url not in cache:
            cache[page_url] = get_soup(session, page_url)
        soup = cache[page_url]
        if fragment:
            candidates = soup.find_all(id=fragment)
            texts = [clean_text(node) for node in candidates]
            texts = [text for text in texts if text]
            if texts:
                return max(texts, key=len)
        return clean_text(soup.select_one('main'))
    except requests.RequestException as error:
        log_message(
            'Failed to scrape Project [BLANK] event detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


def make_records(title, dates, url, venue, description, time_from=None):
    if not title or not url or not venue:
        return []
    return [
        {
            'title': title.strip(' ,'),
            'date': event_date.isoformat(),
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def current_records(session, cache):
    soup = get_soup(session, SOURCE_URL)
    records = []

    for item in soup.select('li.list-item'):
        title = clean_text(item.select_one('.list-item-content__title'))
        text = clean_text(item.select_one('.list-item-content__description'))
        link = item.select_one('a[href]')
        dates = current_dates(text)
        if not title or not link or not dates:
            continue
        url = urljoin(SOURCE_URL, link.get('href'))
        detail = detail_text(session, cache, url)
        records.extend(make_records(
            title,
            dates,
            url,
            parse_venue(detail or '') or 'Bread & Salt Gallery',
            detail or text,
            parse_time(detail or ''),
        ))

    for section in soup.select('section.page-section'):
        text = clean_text(section)
        if not re.search(r'(?:→|\bat\b).*(?:Gallery|Cathedral)', text, re.IGNORECASE):
            continue
        link = section.select_one('a[href]')
        heading = section.select_one('h1, h2, h3')
        if not link or not heading:
            continue
        url = urljoin(SOURCE_URL, link.get('href'))
        year_hint = re.search(r'(20\d{2})', url)
        dates = current_dates(text, year_hint.group(1) if year_hint else None)
        venue = parse_venue(text)
        if not dates or not venue:
            continue
        description = detail_text(session, cache, url) or text
        records.extend(make_records(
            clean_text(heading), dates, url, venue, description, parse_time(text)
        ))
    return records


def archive_records(session, cache):
    soup = get_soup(session, ARCHIVE_URL)
    records = []
    for heading in soup.select('main h2'):
        season = re.fullmatch(r'\s*(20\d{2})\s*/\s*(20\d{2})\s*', clean_text(heading))
        if not season:
            continue
        start_year, end_year = map(int, season.groups())
        for item in heading.find_next_siblings('p'):
            text = clean_text(item)
            dates = archive_dates(text, start_year, end_year)
            link = item.select_one('a[href]')
            venue = parse_venue(text)
            if not dates or not link or not venue:
                continue
            title_node = item.find('strong')
            title = clean_text(title_node)
            if title.endswith(':'):
                remainder = re.sub(r'^.*?:', '', re.sub(r'^\s*\S+\s*', '', text), count=1)
                title = f'{title} {remainder.split(",", 1)[0]}'.strip()
            url = urljoin(ARCHIVE_URL, link.get('href'))
            description = detail_text(session, cache, url) or text
            records.extend(make_records(
                title, dates, url, venue, description, parse_time(description)
            ))
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    cache = {}
    records = current_records(session, cache) + archive_records(session, cache)
    unique = {
        (record['title'].casefold(), record['date'], record['time_from'], record['venue'].casefold()): record
        for record in records
    }
    return sorted(unique.values(), key=lambda record: (record['date'], record['time_from'] or '', record['title']))


class ProjectBlankSdOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='projectblanksd_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ProjectBlankSdOrgCrawler().run()


if __name__ == '__main__':
    main()
