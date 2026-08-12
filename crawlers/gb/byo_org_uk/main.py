import re
from datetime import date, timedelta
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.byo.org.uk/'
SOURCE = 'British Youth Opera'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            '', 'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        )
    )
    if name
}

# These are the performance venues found on BYO's retained event pages.  The
# explicit mapping also covers pages which publish only a postcode, rather than
# repeating London as part of the address.
VENUES = {
    'battersea arts centre': ('Battersea Arts Centre', 'London'),
    'cadogan hall': ('Cadogan Hall', 'London'),
    'saffron hall': ('Saffron Hall', 'Saffron Walden'),
    'smith square hall': ('Smith Square Hall', 'London'),
    'st clement danes church': ('St Clement Danes Church', 'London'),
    '1901 arts club': ('1901 Arts Club', 'London'),
}

DATE_PATTERN = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+)?'
    r'(\d{1,2})(?:st|nd|rd|th)?'
    r'(?:\s*[\u2013-]\s*(\d{1,2})(?:st|nd|rd|th)?)?\s+'
    r'(' + '|'.join(MONTHS) + r')\s+(20\d{2})',
    re.IGNORECASE,
)

NON_EVENT_PATHS = {
    '', 'about', 'administrator', 'byo-hub', 'chair', 'contact',
    'content-freelancer', 'donate', 'home', 'news', 'store', 'support',
    'whats-on', 'work-with-us',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def expand_dates(match):
    start_day = int(match.group(1))
    end_day = int(match.group(2) or start_day)
    month = MONTHS[match.group(3).lower()]
    year = int(match.group(4))
    try:
        start = date(year, month, start_day)
        end = date(year, month, end_day)
    except ValueError:
        return []
    if end < start or (end - start).days > 31:
        return []
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


def parse_time(text):
    # A reception is not the advertised performance start.
    labelled = re.search(
        r'\b(?:recital|concert|performance|opera)\s+(?:at\s+)?'
        r'(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b',
        text,
        re.IGNORECASE,
    )
    match = labelled or re.search(
        r'\b(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b', text, re.IGNORECASE
    )
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def page_location(text):
    lowered = text.lower()
    for needle, location in VENUES.items():
        if needle in lowered:
            return location
    return None


def page_title(soup):
    heading = soup.select_one('main h1')
    if heading and clean_text(heading):
        return clean_text(heading)
    meta = soup.select_one('meta[property="og:title"]')
    title = clean_text(meta.get('content')) if meta else ''
    return re.sub(r'\s+[\u2014-]\s+British Youth Opera\s*$', '', title).strip()


def parse_event_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main#page') or soup.select_one('main')
    if main is None:
        return []
    text = clean_text(main)
    title = page_title(soup)
    date_match = DATE_PATTERN.search(text)
    location = page_location(text)
    if not title or not date_match or not location:
        return []

    # Custom event pages consistently contain a booking call-to-action, or an
    # explicit recital/performance heading.  This excludes dated job and news
    # pages which happen to mention one of the venues.
    booking_text = ' '.join(clean_text(link) for link in main.select('a'))
    evidence = f'{title} {booking_text}'.lower()
    if not re.search(r'book|ticket|recital|concert|opera|progress|grimes', evidence):
        return []

    venue, city = location
    description = text or None
    records = []
    for event_date in expand_dates(date_match):
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(text[:1000]),
            'venue': venue,
            'city': city,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    # Some production pages advertise a second, separately ticketed venue in
    # prose.  Preserve that concrete occurrence too.
    for match in DATE_PATTERN.finditer(text[date_match.end():]):
        context = text[max(0, match.start() + date_match.end() - 120):
                       match.end() + date_match.end() + 160]
        secondary_location = page_location(context)
        if not secondary_location or secondary_location == location:
            continue
        secondary_venue, secondary_city = secondary_location
        for event_date in expand_dates(match):
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(context),
                'venue': secondary_venue,
                'city': secondary_city,
                'country_code': 'GB',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    # BYO occasionally omits the repeated year for an additional performance
    # described on the same production page (for example, "Saturday 5 July at
    # Saffron Hall").  It is safe to inherit the production year here.
    short_pattern = re.compile(
        r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
        r'(\d{1,2})(?:st|nd|rd|th)?\s+(' + '|'.join(MONTHS) + r')\b',
        re.IGNORECASE,
    )
    primary_year = int(date_match.group(4))
    for match in short_pattern.finditer(text[date_match.end():]):
        absolute_start = match.start() + date_match.end()
        absolute_end = match.end() + date_match.end()
        context = text[max(0, absolute_start - 120):absolute_end + 180]
        secondary_location = page_location(context)
        if not secondary_location or secondary_location == location:
            continue
        try:
            event_date = date(
                primary_year,
                MONTHS[match.group(2).lower()],
                int(match.group(1)),
            ).isoformat()
        except ValueError:
            continue
        secondary_venue, secondary_city = secondary_location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(context),
            'venue': secondary_venue,
            'city': secondary_city,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class ByoOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='byo_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SITEMAP_URL, timeout=60)
            response.raise_for_status()
            root = ElementTree.fromstring(response.text)
        except (requests.RequestException, ElementTree.ParseError) as error:
            log_message(
                'Failed to fetch BYO sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        namespace = {'sitemap': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        urls = [node.text for node in root.findall('.//sitemap:loc', namespace) if node.text]
        records = []
        for url in urls:
            path = urlparse(url).path.strip('/')
            if path in NON_EVENT_PATHS or path.startswith(('articles', 'blog-3')):
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_event_page(url, response.text))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch BYO page',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    ByoOrgUkCrawler().run()


if __name__ == '__main__':
    main()
