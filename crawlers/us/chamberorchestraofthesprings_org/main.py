import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chamberorchestraofthesprings.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Chamber Orchestra of the Springs'
CITY = 'Colorado Springs'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH_PATTERN = (
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
    r'Nov(?:ember)?|Dec(?:ember)?)'
)
DATE_RE = re.compile(
    rf'\b({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*(?:[-–]|&)\s*'
    rf'(?:{MONTH_PATTERN}\s+)?(\d{{1,2}})(?:st|nd|rd|th)?)?,?\s+(20\d{{2}})\b',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?\b', re.I)
SHORT_DATE_RE = re.compile(rf'\b({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b', re.I)

VENUES = {
    'first united methodist church': 'First United Methodist Church',
    'chapman recital hall': 'Ent Center for the Arts — Chapman Recital Hall',
    'ent center: chapman': 'Ent Center for the Arts — Chapman Recital Hall',
    'ent center: shockley': 'Ent Center for the Arts — Shockley-Zalabak Theater',
    'shockley zalabak theater': 'Ent Center for the Arts — Shockley-Zalabak Theater',
    'ent center for the arts': 'Ent Center for the Arts',
}

SEASON_PATH_RE = re.compile(r'^/(?:20\d{2}-20?\d{2}|20\d{2}-\d{2})/?$')
NON_EVENT_PATHS = {
    '/', '/home', '/mission', '/our-musicians', '/our-staff', '/calendar',
    '/our-venues', '/donate', '/press-room', '/what-to-expect', '/listen',
    '/discover', '/engage', '/support', '/store', '/opera2030',
    '/educational-programming', '/emerging-soloist', '/radio-broadcasts',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date_parts(match):
    month, first_day, second_day, year = match.groups()
    if month.lower() == 'sept':
        month = 'Sep'
    values = []
    for day in (first_day, second_day):
        if not day:
            continue
        try:
            values.append(
                datetime.strptime(f'{month} {day} {year}', '%b %d %Y').date().isoformat()
            )
        except ValueError:
            try:
                values.append(
                    datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
                )
            except ValueError:
                continue
    return values


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
    return f'{hour:02d}:{minute or "00"}'


def find_venue(value):
    lowered = value.lower()
    for needle, venue in VENUES.items():
        if needle in lowered:
            return venue
    return ''


def content_lines(soup):
    main = soup.select_one('main') or soup
    lines = []
    for node in main.select('h1, h2, h3, h4, p'):
        text = clean_text(node.get_text(' ', strip=True))
        if text and (not lines or text != lines[-1]):
            lines.append((node.name, text))
    return lines


def page_description(lines, cutoff):
    parts = []
    for _, text in lines[1:cutoff]:
        if DATE_RE.search(text) or text.lower().startswith(('you’ll see:', "you'll see:")):
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    lines = content_lines(soup)
    if not lines:
        return []
    title = next((text for tag, text in lines if tag == 'h1'), '')
    if not title:
        title = clean_text(soup.title.get_text(' ', strip=True)).split(' — ')[0]
    if not title:
        return []

    marker = next(
        (i for i, (_, text) in enumerate(lines) if text.lower().startswith(
            ('choose your date', 'one day only', 'concert has concluded')
        )),
        None,
    )
    if marker is None and urlparse(url).path.rstrip('/') == '/frankenstein':
        # Some special-production pages omit the year, but identify the active
        # season in their first-party navigation (for example, "2026-27").
        season_link = soup.find('a', href=re.compile(r'^/20\d{2}-\d{2}/?$'))
        for index, (_, text) in enumerate(lines):
            match = SHORT_DATE_RE.search(text)
            nearby = ' '.join(item[1] for item in lines[index:index + 3])
            venue = find_venue(nearby)
            time_from = parse_time(nearby)
            if not match or not venue or not time_from or not season_link:
                continue
            start_year = int(re.search(r'20\d{2}', season_link['href']).group())
            month = match.group(1)
            if month.lower() == 'sept':
                month = 'Sep'
            try:
                month_number = datetime.strptime(month, '%b').month
            except ValueError:
                month_number = datetime.strptime(month, '%B').month
            year = start_year + (month_number < 7)
            try:
                event_date = datetime(year, month_number, int(match.group(2))).date().isoformat()
            except ValueError:
                return []
            return [make_record(
                title, event_date, url, time_from, venue,
                page_description(lines, index),
            )]
        return []
    if marker is None:
        return []

    description = page_description(lines, marker)
    records = []
    for index in range(marker + 1, min(marker + 12, len(lines))):
        text = lines[index][1]
        if text.lower().startswith(('more than a concert', 'this concert’s', "this concert's")):
            break
        match = DATE_RE.search(text)
        if not match:
            continue
        nearby = ' '.join(item[1] for item in lines[index:index + 3])
        venue = find_venue(nearby)
        if not venue:
            continue
        time_from = parse_time(nearby)
        for event_date in parse_date_parts(match):
            records.append(make_record(title, event_date, url, time_from, venue, description))
    return records


def parse_season_page(html, url):
    lines = content_lines(BeautifulSoup(html, 'html.parser'))
    records = []
    for index, (tag, title) in enumerate(lines):
        if tag not in {'h2', 'h3'}:
            continue
        end = next(
            (i for i in range(index + 1, len(lines)) if lines[i][0] in {'h2', 'h3'}),
            min(index + 14, len(lines)),
        )
        section = ' '.join(text for _, text in lines[index + 1:end])
        match = DATE_RE.search(section)
        venue = find_venue(section)
        if not match or not venue:
            continue
        description_parts = [
            text for _, text in lines[index + 1:end]
            if not DATE_RE.search(text) and not text.startswith('#')
        ]
        description = '\n\n'.join(description_parts) or None
        for event_date in parse_date_parts(match):
            records.append(make_record(title, event_date, url, None, venue, description))
    return records


def make_record(title, event_date, url, time_from, venue, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for node in soup.select('url > loc'):
        url = clean_text(node.get_text())
        path = urlparse(url).path.rstrip('/') or '/'
        if path not in NON_EVENT_PATHS and not path.startswith('/store/'):
            urls.append(url)
    return urls


def fetch_and_parse(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    path = urlparse(url).path.rstrip('/') or '/'
    if SEASON_PATH_RE.fullmatch(path):
        return parse_season_page(response.text, url)
    return parse_detail_page(response.text, url)


class ChamberOrchestraOfTheSpringsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chamberorchestraofthesprings_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = sitemap_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_and_parse, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Chamber Orchestra concert page',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        unique = {}
        for record in records:
            key = (record['title'].lower(), record['date'], record['time_from'], record['venue'])
            unique[key] = record
        return sorted(
            unique.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    ChamberOrchestraOfTheSpringsOrgCrawler().run()


if __name__ == '__main__':
    main()
