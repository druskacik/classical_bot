import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://dallasopera.org/'
SOURCE = 'The Dallas Opera'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/performance'
VENUE = 'Margot and Bill Winspear Opera House'
CITY = 'Dallas'
TYPE_IDS = (2626, 2627, 2628)  # Mainstage, Family, Concerts
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}
MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        (
            (), ('jan', 'january'), ('feb', 'february'), ('mar', 'march'),
            ('apr', 'april'), ('may',), ('jun', 'june'), ('jul', 'july'),
            ('aug', 'august'), ('sep', 'sept', 'september'), ('oct', 'october'),
            ('nov', 'november'), ('dec', 'december'),
        )
    )
    for name in names
}
MONTH_PATTERN = re.compile(
    r'\b(' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + r')\.?\b', re.I
)


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', value, re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def parse_full_occurrences(soup):
    """Prefer the site's ticket rows, which contain unambiguous full dates."""
    records = []
    for row in soup.select('table tr'):
        text = clean_text(row.get_text(' ', strip=True))
        match = re.search(
            r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
            r'([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})',
            text,
            re.I,
        )
        if not match:
            continue
        month = MONTHS.get(match.group(1).lower().rstrip('.'))
        if not month:
            continue
        try:
            date = datetime(int(match.group(3)), month, int(match.group(2))).date().isoformat()
        except ValueError:
            continue
        records.append((date, parse_time(text)))
    return list(dict.fromkeys(records))


def parse_compact_dates(value):
    """Parse the compact date lists used by archived Dallas Opera pages."""
    text = clean_text(value)
    text = re.sub(r'\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b', '', text, flags=re.I)
    matches = list(MONTH_PATTERN.finditer(text))
    if not matches:
        return []

    explicit_years = [(match.start(), int(match.group())) for match in re.finditer(r'\b20\d{2}\b', text)]
    if not explicit_years:
        return []

    groups = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end():end]
        days = [int(day) for day in re.findall(r'(?<![:\d])(\d{1,2})(?![:\d])', segment)]
        days = [day for day in days if 1 <= day <= 31]
        following = [year for position, year in explicit_years if position >= match.end()]
        year = following[0] if following else explicit_years[-1][1]
        groups.append([MONTHS[match.group().lower().rstrip('.')], year, days])

    # If one final year labels a list crossing New Year, adjust earlier months.
    for index in range(len(groups) - 2, -1, -1):
        if groups[index][0] > groups[index + 1][0] and groups[index][1] == groups[index + 1][1]:
            groups[index][1] -= 1

    dates = []
    for month, year, days in groups:
        for day in days:
            try:
                dates.append(datetime(year, month, day).date().isoformat())
            except ValueError:
                continue
    return list(dict.fromkeys(dates))


def infer_venue_and_city(soup):
    text = clean_text(soup.get_text(' ', strip=True))
    known = (
        ('National Shrine Cathedral of Our Lady of Guadalupe', 'Dallas'),
        ('Cathedral Shrine of the Virgin of Guadalupe', 'Dallas'),
        ('Moody Performance Hall', 'Dallas'),
        ('Winspear Opera House', 'Dallas'),
        ('Strauss Square', 'Dallas'),
    )
    for venue, city in known:
        if venue.lower() in text.lower():
            return venue, city
    return VENUE, CITY


def parse_performance(item):
    url = item.get('link') or ''
    title = clean_text((item.get('title') or {}).get('rendered'))
    content = (item.get('content') or {}).get('rendered') or ''
    soup = BeautifulSoup(content, 'html.parser')
    occurrences = parse_full_occurrences(soup)
    if not occurrences:
        dates = parse_compact_dates((item.get('meta') or {}).get('performance_dates'))
        time_from = parse_time(clean_text((item.get('meta') or {}).get('performance_dates')))
        occurrences = [(date, time_from) for date in dates]

    venue, city = infer_venue_and_city(soup)
    description_parts = [
        clean_text((item.get('meta') or {}).get('performance_composer')),
        clean_text(content),
    ]
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None
    if not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        return []

    return [
        {
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for date, time_from in occurrences
    ]


class DallasoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dallasopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        items = []
        page = 1
        total_pages = 1
        try:
            while page <= total_pages:
                response = session.get(
                    API_URL,
                    params={
                        'tdo_type': ','.join(map(str, TYPE_IDS)),
                        'per_page': 100,
                        'page': page,
                    },
                    timeout=45,
                )
                response.raise_for_status()
                items.extend(response.json())
                total_pages = int(response.headers.get('X-WP-TotalPages', 1))
                page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Dallas Opera performances',
                event='crawler_listing_request_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        records = []
        for item in items:
            records.extend(parse_performance(item))

        if not records:
            log_message(
                'No Dallas Opera performance occurrences found',
                event='crawler_empty_listing',
                level='warning',
                url=API_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    DallasoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
