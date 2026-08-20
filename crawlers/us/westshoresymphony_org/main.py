import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.westshoresymphony.org/'
SOURCE = 'West Shore Symphony Orchestra'
CITY = 'Camp Hill'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

EVENT_PAGE_LABELS = {
    'MASTERWORKS': 'masterworks',
    'CHAMBER MUSIC': 'chamber',
    'CONCERTO COMPETITION': 'competition',
    'EDUCATION': 'education',
}

MONTH_DATE_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|October|November|December) '
    r'(\d{1,2})$'
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ').replace('\u200b', '')).strip()


def page_lines(html):
    soup = BeautifulSoup(html, 'html.parser')
    return [clean_text(line) for line in soup.get_text('\n').splitlines() if clean_text(line)]


def parse_date(month, day, year):
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_clock(value, default_pm=False):
    value = clean_text(value).lower().replace('.', '')
    match = re.search(r'\b(\d{1,2})(?::([0-5]\d))?\s*([ap]m)?\b', value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)
    if suffix:
        hour %= 12
        if suffix == 'pm':
            hour += 12
    elif default_pm and 1 <= hour <= 7:
        hour += 12
    if hour > 23:
        return None
    return f'{hour:02d}:{minute:02d}'


def record(title, event_date, url, time_from, venue, city, description):
    if not all((title, event_date, url, venue, city)):
        return None
    title = clean_text(title)
    if len(title) >= 2 and title[0] in '"“' and title[-1] in '"”':
        title = title[1:-1].strip()
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': clean_text(venue),
        'city': clean_text(city),
        'country_code': 'US',
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_masterworks(html, url):
    lines = page_lines(html)
    season = next(
        (match for line in lines if (match := re.search(r'Masterworks\s+(20\d{2})-(\d{2})', line, re.I))),
        None,
    )
    if not season:
        return []
    first_year = int(season.group(1))
    default_time = next(
        (parse_clock(line, default_pm=True) for line in lines if re.search(r'Sundays? at', line, re.I)),
        None,
    )
    date_indexes = [(index, MONTH_DATE_RE.fullmatch(line)) for index, line in enumerate(lines)]
    date_indexes = [(index, match) for index, match in date_indexes if match]
    records = []
    for position, (index, match) in enumerate(date_indexes):
        end = date_indexes[position + 1][0] if position + 1 < len(date_indexes) else len(lines)
        end = next((i for i in range(index + 1, end) if lines[i].startswith('275 Cumberland')), end)
        segment = lines[index + 1:end]
        if len(segment) < 3:
            continue
        month, day = match.groups()
        year = first_year if datetime.strptime(month, '%B').month >= 7 else first_year + 1
        event_date = parse_date(month, day, year)
        title = segment[0]
        venue = segment[1]
        location = segment[2]
        city_match = re.search(r',\s*([^,]+)$', location)
        city = city_match.group(1) if city_match else CITY
        description = ' '.join(segment[3:])
        item = record(title, event_date, url, default_time, venue, city, description)
        if item:
            records.append(item)
    return records


def parse_chamber(html, url):
    lines = page_lines(html)
    season = next(
        (match for line in lines if (match := re.search(r'Summer\s+(20\d{2})\s+Season', line, re.I))),
        None,
    )
    if not season:
        return []
    year = int(season.group(1))
    event_re = re.compile(
        r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
        r'([A-Za-z]+)\s+(\d{1,2}),\s*(.+)$'
    )
    records = []
    for index, line in enumerate(lines):
        match = event_re.match(line)
        if not match or index + 1 >= len(lines):
            continue
        month, day, title = match.groups()
        location = lines[index + 1]
        location_match = re.match(
            r'(.+?)\s+(Peace Church|Amelia Given Library)(?:,.*)?,\s*([^,]+)$', location, re.I
        )
        if not location_match:
            continue
        time_text, venue, city = location_match.groups()
        item = record(
            title, parse_date(month, day, year), url,
            parse_clock(time_text, default_pm=True), venue, city, None,
        )
        if item:
            records.append(item)
    return records


def parse_competition(html, url):
    lines = page_lines(html)
    title = next((line for line in lines if re.search(r'20\d{2} Annual Concerto Competition', line)), '')
    year_match = re.search(r'(20\d{2})', title)
    date_line = next((line for line in lines if re.search(r'January\s+\d{1,2}', line)), '')
    date_match = re.search(r'January\s+(\d{1,2})', date_line)
    if not year_match or not date_match:
        return []
    event_date = parse_date('January', date_match.group(1), int(year_match.group(1)))
    description_start = lines.index(title) if title in lines else 0
    description_end = next(
        (index for index in range(description_start, len(lines)) if lines[index].startswith('275 Cumberland')),
        len(lines),
    )
    description = ' '.join(lines[description_start:description_end])
    item = record(title, event_date, url, None, 'Stretansky Hall, Susquehanna University', 'Selinsgrove', description)
    return [item] if item else []


def parse_education(html, url):
    lines = page_lines(html)
    title_index = next((index for index, line in enumerate(lines) if line.strip('"“”') == 'Orchestra Animals'), None)
    if title_index is None:
        return []
    date_index = next((index for index in range(title_index + 1, len(lines)) if re.search(r'20\d{2}', lines[index])), None)
    if date_index is None:
        return []
    date_match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2}),\s*(.+)$', lines[date_index])
    if not date_match or date_index + 1 >= len(lines):
        return []
    month, day, year, venue = date_match.groups()
    city_match = re.search(r',\s*([^,]+)$', lines[date_index + 1])
    city = city_match.group(1) if city_match else CITY
    event_date = parse_date(month, day, int(year))
    times = []
    for line in lines[date_index + 2:date_index + 10]:
        if re.fullmatch(r'\d{1,2}:\d{2}', line):
            times.append(parse_clock(line))
    about_index = next((i for i in range(date_index, len(lines)) if lines[i] == 'About the program'), None)
    reserve_index = next((i for i in range(date_index, len(lines)) if lines[i] == 'Reserve Seats for'), len(lines))
    description = ' '.join(lines[about_index + 1:reserve_index]) if about_index is not None else None
    return [record('Orchestra Animals', event_date, url, value, venue, city, description) for value in times]


PARSERS = {
    'masterworks': parse_masterworks,
    'chamber': parse_chamber,
    'competition': parse_competition,
    'education': parse_education,
}


class WestShoreSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='westshoresymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
            soup = BeautifulSoup(home_response.text, 'html.parser')
            pages = {}
            for link in soup.find_all('a', href=True):
                label = clean_text(link.get_text(' ', strip=True)).upper()
                for prefix, page_type in EVENT_PAGE_LABELS.items():
                    if label.startswith(prefix):
                        pages[page_type] = urljoin(SOURCE_URL, link['href'])
            records = []
            for page_type, page_url in pages.items():
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
                records.extend(PARSERS[page_type](response.text, page_url))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch West Shore Symphony event pages',
                event='crawler_fetch_failed',
                level='error',
                url=getattr(getattr(error, 'request', None), 'url', SOURCE_URL),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not records:
            log_message(
                'No West Shore Symphony concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    WestShoreSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
