import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://anchorageopera.org/'
SOURCE = 'Anchorage Opera'
SEASON_URL = urljoin(SOURCE_URL, '2026-2027-season/')
SPOTLIGHT_URL = urljoin(SOURCE_URL, 'spotlight-events/')
SITEMAP_URL = urljoin(SOURCE_URL, 'page-sitemap.xml')
CITY = 'Anchorage'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|'
    'Dec(?:ember)?'
)
DATE_RE = re.compile(
    rf'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,.]?\s+)?'
    rf'({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_time(value):
    range_match = re.search(
        r'\b(\d{1,2})(?::(\d{2}))?\s*[\u2013\u2014-]\s*'
        r'\d{1,2}(?::\d{2})?\s*([AP]M)\b',
        value or '',
        re.I,
    )
    if range_match:
        hour, minute, meridiem = range_match.groups()
        return datetime.strptime(
            f'{hour}:{minute or "00"} {meridiem.upper()}', '%I:%M %p'
        ).strftime('%H:%M')
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    return datetime.strptime(
        f'{hour}:{minute or "00"} {meridiem.upper()}', '%I:%M %p'
    ).strftime('%H:%M')


def parse_full_dates(value):
    dates = []
    for month, day, year in DATE_RE.findall(value or ''):
        try:
            dates.append(
                datetime.strptime(f'{month[:3]} {day} {year}', '%b %d %Y')
                .date()
                .isoformat()
            )
        except ValueError:
            continue
    return dates


def expand_schedule_dates(schedule, year):
    """Expand `November 12, 14, and 15` style first-party schedules."""
    month_match = re.search(rf'({MONTHS})', schedule, re.I)
    if not month_match:
        return []
    month = month_match.group(1)
    tail = schedule[month_match.end():]
    tail = re.split(r'\b(?:at|by|in)\b', tail, maxsplit=1, flags=re.I)[0]
    days = re.findall(r'\b([0-3]?\d)\b', re.sub(r'\b20\d{2}\b', '', tail))
    parsed = []
    for day in days:
        try:
            parsed.append(
                datetime.strptime(f'{month[:3]} {day} {year}', '%b %d %Y')
                .date()
                .isoformat()
            )
        except ValueError:
            continue
    return list(dict.fromkeys(parsed))


def available_season_urls(session):
    """Return every still-published first-party season overview."""
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for loc in soup.find_all('loc'):
        url = clean_text(loc)
        if re.fullmatch(
            r'https://anchorageopera\.org/(?:20\d{2}(?:-20\d{2}|-\d{2})-season)/',
            url,
        ):
            urls.append(url)
    if SEASON_URL not in urls:
        urls.append(SEASON_URL)
    return sorted(set(urls))


def find_venue(text):
    patterns = [
        r'Location:\s*([^\n]{2,80}\b(?:Theatre|Theater|Atrium|Studio|Library|UAA))',
        r'\b(Anchorage Museum Atrium|Loussac Library|Harper Studio)\b',
        r'\b(Discovery Theatre|Sydney Laurence Theatre|Wilda Marston Theatre|The Nave)\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            venue = clean_text(match.group(1))
            venue = re.sub(r'^Location:\s*', '', venue, flags=re.I)
            if venue:
                return venue
    return ''


def season_records(session, season_url):
    soup = get_soup(session, season_url)
    records = []
    for row in soup.select('article .et_pb_row'):
        text = clean_text(row)
        date_heading = next(
            (node for node in row.select('h1, h2, h3, h4') if re.search(r'\b20\d{2}\b', clean_text(node))),
            None,
        )
        year_match = re.search(r'\b(20\d{2})\b', clean_text(date_heading)) if date_heading else None
        link = next(
            (a for a in row.select('a[href]') if 'learn more' in clean_text(a).lower()),
            None,
        )
        heading = next(
            (node for node in row.select('h1, h2, h3') if clean_text(node) and node is not date_heading),
            None,
        )
        if heading is None and date_heading is not None:
            heading = date_heading
        if not year_match or not link or not heading:
            continue
        heading_text = clean_text(heading)
        title = re.split(
            rf'\s+[\u2013\u2014-]\s+(?=(?:{MONTHS})\b)', heading_text, maxsplit=1, flags=re.I
        )[0].strip()
        url = urljoin(season_url, link.get('href'))
        dates = expand_schedule_dates(clean_text(date_heading), int(year_match.group(1)))
        if not title or not dates:
            continue

        detail_text = ''
        try:
            detail_text = clean_text(get_soup(session, url).select_one('article'))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
        venue = find_venue(detail_text)
        if not venue:
            continue

        schedule_lines = [line for line in detail_text.splitlines() if DATE_RE.search(line)]
        schedule_by_date = {}
        for line in schedule_lines:
            for event_date in parse_full_dates(line):
                schedule_by_date[event_date] = parse_time(line)
        # Detail pages omit the year from their performance list. Match times
        # in listing order when explicit date/year pairs are unavailable.
        listed_times = [parse_time(line) for line in text.splitlines() if TIME_RE.search(line)]
        for index, event_date in enumerate(dates):
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': schedule_by_date.get(event_date) or (
                    listed_times[index] if index < len(listed_times) else None
                ),
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': detail_text or text,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def spotlight_records(session):
    soup = get_soup(session, SPOTLIGHT_URL)
    records = []
    # The first rows are navigation/calendar summaries. Subsequent rows are
    # the first-party event detail cards and include the useful long text.
    for row in soup.select('article .et_pb_row'):
        text = clean_text(row)
        heading = row.select_one('h3, h4, h2, h1')
        dates = parse_full_dates(text)
        if not heading or len(dates) != 1:
            continue
        title = clean_text(heading)
        venue = find_venue(text)
        if not title or not venue:
            continue
        link = row.select_one('a[href]')
        url = urljoin(SPOTLIGHT_URL, link.get('href')) if link else SPOTLIGHT_URL
        records.append({
            'title': title,
            'date': dates[0],
            'url': url,
            'time_from': parse_time(text),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class AnchorageOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='anchorageopera_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for season_url in available_season_urls(session):
            try:
                records.extend(season_records(session, season_url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape season page',
                    event='crawler_page_failed',
                    level='warning',
                    url=season_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        records.extend(spotlight_records(session))
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    AnchorageOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
