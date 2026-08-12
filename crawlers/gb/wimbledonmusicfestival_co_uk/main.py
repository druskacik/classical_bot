import re
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wimbledonmusicfestival.co.uk/'
SOURCE = 'Wimbledon International Music Festival'
ARCHIVE_URL = urljoin(SOURCE_URL, 'wimf-festivals/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|'
    r'Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\s+'
    r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|'
    r'Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b,?',
    re.I,
)
TIME_RE = re.compile(
    r'(?<!\d)([01]?\d|2[0-3])(?:[.:](\d{2}))\s*(am|pm)?(?!\w)', re.I
)
PROGRAMME_RE = re.compile(r'/wimf(20\d{2})/programme/?$', re.I)

# Every published performance venue in these programme archives is in London.
# Canonical names avoid treating street names and postcodes as venue content.
VENUES = (
    (r'upper hall.*sacred heart|sacred heart.*upper hall', 'Upper Hall, Sacred Heart Church'),
    (r'sacred heart church', 'Sacred Heart Church'),
    (r'st john(?:\u2019|\'|’)s (?:church|the baptist)|st john the baptist',
     'St John the Baptist, Wimbledon'),
    (r'st mary(?:\u2019|\'|’)s church', "St Mary's Church, Putney"),
    (r'trinity church', 'Trinity United Reformed Church, Wimbledon'),
    (r'auditorium.*wimbledon high school', 'Auditorium, Wimbledon High School'),
    (r'wimbledon high school', 'Wimbledon High School'),
    (r'merton arts space', 'Merton Arts Space'),
    (r'new wimbledon theatre', 'New Wimbledon Theatre'),
    (r'king(?:\u2019|\'|’)s college school', "King's College School, Wimbledon"),
    (r'holy trinity', 'Holy Trinity Church, Wandsworth'),
    (r'the rushmere', 'The Rushmere'),
    (r'jeroboams', 'Jeroboams, Wimbledon Village'),
    (r'friarwood fine wines', 'Friarwood Fine Wines, Wimbledon Village'),
    (r'the old frizzle', 'The Old Frizzle'),
    (r'light house restaurant', 'Light House Restaurant'),
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = unescape(text).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(max_retries=Retry(
            total=2,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )),
    )
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def programme_pages(session):
    soup = get_soup(session, ARCHIVE_URL)
    pages = []
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', '')).rstrip('/') + '/'
        match = PROGRAMME_RE.search(url.rstrip('/'))
        if match:
            pages.append((int(match.group(1)), url))
    return sorted(set(pages))


def parse_date(match, year):
    from datetime import datetime
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)[:3]} {year}', '%d %b %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_times(value):
    lowered = value.lower()
    if 'all day' in lowered or 'daytime' in lowered:
        return [None]
    times = []
    matches = list(TIME_RE.finditer(value))
    for index, match in enumerate(matches):
        if index:
            separator = value[matches[index - 1].end():match.start()]
            if re.search(r'(?:-|–|—|\bto\b)', separator, re.I):
                # The second time is an end time, not another occurrence.
                continue
        hour = int(match.group(1))
        minute = int(match.group(2))
        suffix = (match.group(3) or '').lower()
        if suffix and not 1 <= hour <= 12:
            continue
        if suffix == 'pm' and hour != 12:
            hour += 12
        elif suffix == 'am' and hour == 12:
            hour = 0
        formatted = f'{hour:02d}:{minute:02d}'
        if formatted not in times:
            times.append(formatted)
    return times or [None]


def resolve_venue(value):
    normalized = clean_text(value).replace('\n', ' ')
    for pattern, venue in VENUES:
        if re.search(pattern, normalized, re.I):
            return venue
    return None


def row_title(row):
    heading = row.select_one('h1, h2, h3, h4, h5')
    if heading:
        return clean_text(heading).replace('\n', ' ')
    for module in row.select('.et_pb_text'):
        text = clean_text(module).replace('\n', ' ')
        if text and not DATE_RE.search(text) and not resolve_venue(text):
            return text
    return ''


def detail_description(session, url):
    try:
        soup = get_soup(session, url)
    except requests.RequestException as error:
        log_message(
            'Failed to fetch WIMF event detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None
    content = soup.select_one('.et_pb_post_content')
    return clean_text(content) or None


def parse_programme(session, year, url):
    soup = get_soup(session, url)
    records = []
    for row in soup.select('.et_pb_row'):
        text = clean_text(row).replace('\n', ' ')
        date_match = DATE_RE.search(text)
        if not date_match or 'postponed to' in text.lower():
            continue

        event_date = parse_date(date_match, year)
        title = row_title(row)
        venue = resolve_venue(text[date_match.end():])
        if not title or not event_date or not venue:
            continue

        detail_link = row.select_one(
            f'a[href*="/wimf{year}-programme/"][href], '
            f'a[href*="/wimf{year}/programme/"][href]'
        )
        event_url = urljoin(SOURCE_URL, detail_link['href']) if detail_link else url
        description = detail_description(session, event_url) if detail_link else text
        for time_from in parse_times(text[date_match.end():]):
            records.append({
                'title': title,
                'date': event_date,
                'url': event_url,
                'time_from': time_from,
                'venue': venue,
                'city': 'London',
                'country_code': 'GB',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class WimbledonMusicFestivalCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wimbledonmusicfestival_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        try:
            pages = programme_pages(session)
            records = []
            for year, url in pages:
                records.extend(parse_programme(session, year, url))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch WIMF programme archive',
                event='crawler_fetch_failed',
                level='error',
                url=ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return sorted(records, key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['venue']
        ))


def main():
    WimbledonMusicFestivalCoUkCrawler().run()


if __name__ == '__main__':
    main()
