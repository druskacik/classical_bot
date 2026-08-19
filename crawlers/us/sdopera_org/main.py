import re
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sdopera.org/'
SITEMAP_URL = f'{SOURCE_URL}shows-sitemap.xml'
SOURCE = 'San Diego Opera'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH = (
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|'
    r'Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
)
WEEKDAY = r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
FULL_DATE_RE = re.compile(
    rf'^{WEEKDAY},?\s+(?P<month>{MONTH})\s+(?P<day>\d{{1,2}}),?\s+'
    rf'(?P<year>20\d{{2}})(?:\s+at(?:\s+(?P<time>\d{{1,2}}:\d{{2}}\s*[ap]m))?'
    rf'|\s+(?P<plain_time>\d{{1,2}}:\d{{2}}\s*[ap]m))?'
    r'(?:\s+at\s+(?P<venue>.+?))?\.?$',
    re.IGNORECASE,
)
CARD_DATE_RE = re.compile(
    rf'^(?P<month>{MONTH})\s+(?P<day>\d{{1,2}})(?:,?\s+(?P<year>20\d{{2}}))?$',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'^(?:at\s+)?(?P<time>\d{1,2}:\d{2}\s*[ap]m)$', re.IGNORECASE)
YEAR_RE = re.compile(r'\b(20\d{2})\b')


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    text = (
        BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
        if '<' in value
        else unescape(value)
    )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def show_urls(session):
    soup = get_soup(session, SITEMAP_URL, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        path = urlparse(url).path
        if path.startswith('/shows/') and path.count('/') == 3:
            urls.append(url)
    return list(dict.fromkeys(urls))


def page_lines(soup):
    copy = BeautifulSoup(str(soup), 'html.parser')
    for node in copy.select('script, style, noscript, header, nav, footer'):
        node.decompose()
    return [
        clean_text(line)
        for line in copy.get_text('\n', strip=True).splitlines()
        if clean_text(line)
    ]


def parse_datetime(month, day, year, time_value):
    time_value = re.sub(r'\s+', '', time_value or '').upper()
    value = f'{month} {day} {year}'
    for date_format in ('%B %d %Y', '%b %d %Y'):
        try:
            event_date = datetime.strptime(value, date_format).date().isoformat()
            break
        except ValueError:
            continue
    else:
        return None

    if not time_value:
        return event_date, None
    try:
        event_time = datetime.strptime(time_value, '%I:%M%p').strftime('%H:%M')
    except ValueError:
        return None
    return event_date, event_time


def extract_performances(lines):
    # Dates are always near the production heading. Limiting this scan avoids
    # mistaking years and engagements in the extensive artist biographies for
    # performances of the advertised production.
    header = lines[:80]
    overview_year = next(
        (
            match.group(1)
            for line in header[:8]
            if (match := YEAR_RE.search(line))
        ),
        None,
    )
    performances = []

    for index, line in enumerate(header):
        match = FULL_DATE_RE.fullmatch(line)
        if match:
            time_value = match.group('time') or match.group('plain_time')
            if not time_value and index + 1 < len(header):
                time_match = TIME_RE.fullmatch(header[index + 1])
                time_value = time_match.group('time') if time_match else None
            parsed = parse_datetime(
                match.group('month'), match.group('day'), match.group('year'), time_value
            )
            if parsed:
                performances.append((*parsed, clean_text(match.group('venue')) or None))
            continue

        # Newer Elementor pages split each occurrence over weekday, date and
        # time lines. Requiring the weekday immediately before the date makes
        # this distinct from season ranges and prose.
        card_match = CARD_DATE_RE.fullmatch(line)
        if not card_match or index == 0 or not re.fullmatch(WEEKDAY + ',?', header[index - 1], re.I):
            continue
        year = card_match.group('year') or overview_year
        time_match = TIME_RE.fullmatch(header[index + 1]) if index + 1 < len(header) else None
        if not year or not time_match:
            continue
        parsed = parse_datetime(
            card_match.group('month'),
            card_match.group('day'),
            year,
            time_match.group('time'),
        )
        if parsed:
            performances.append((*parsed, None))

    unique = {}
    for performance in performances:
        unique[(performance[0], performance[1])] = performance
    return list(unique.values())


def extract_venue(lines, performance_venues):
    inline = next((venue for venue in performance_venues if venue), None)
    if inline:
        return re.sub(r'[.]$', '', inline).strip()

    header = ' '.join(lines[:35])
    if 'The Baker-Baum Concert Hall' in header:
        if 'The Conrad Prebys Performing Arts Center' in header:
            return 'The Baker-Baum Concert Hall at The Conrad Prebys Performing Arts Center'
        return 'The Baker-Baum Concert Hall'
    for venue in ('San Diego Civic Theatre', 'Balboa Theatre'):
        if venue in header:
            return venue
    return None


def extract_title(soup, lines):
    meta = soup.select_one('meta[property="og:title"]')
    title = clean_text(meta.get('content')) if meta else ''
    if not title and lines:
        title = lines[0]
    return re.sub(r'\s*[|–-]\s*San Diego Opera\s*$', '', title).strip()


def parse_show(session, url):
    soup = get_soup(session, url)
    lines = page_lines(soup)
    title = extract_title(soup, lines)
    performances = extract_performances(lines)
    venue = extract_venue(lines, [item[2] for item in performances])
    if not title or not performances or not venue:
        return []

    city = 'La Jolla' if any('La Jolla' in line for line in lines[:35]) else 'San Diego'
    description = '\n'.join(lines) or None
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': performance_venue or venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from, performance_venue in performances
    ]


def get_concerts():
    session = build_session()
    urls = show_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_show, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class SdoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sdopera_org',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SdoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
