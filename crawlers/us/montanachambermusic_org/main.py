import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://montanachambermusic.org/'
SOURCE = 'Montana Chamber Music'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concert-page'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
OCCURRENCE_RE = re.compile(
    rf'(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?'
    r'(?:,)?(?:\s+(?P<year>20\d{2}))?.{0,16}?'
    r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm|noon)',
    re.IGNORECASE,
)

CITY_NAMES = (
    'White Sulphur Springs',
    'Idaho Falls',
    'Big Timber',
    'St. Ignatius',
    'Livingston',
    'Lewistown',
    'Missoula',
    'Whitefish',
    'Belgrade',
    'Cardwell',
    'Bozeman',
    'Butte',
    'Helena',
    'Manhattan',
    'Whitehall',
    'Basin',
    'Pony',
)

# Some detail cards name only the venue. These mappings are supported by other
# first-party concert pages which publish the same venue with its locality.
VENUE_CITIES = {
    'aspevig studio': 'Bozeman',
    'country bookshelf': 'Bozeman',
    'reynolds recital hall': 'Bozeman',
    'msu reynolds recital hall': 'Bozeman',
    'reynolds recital hall at howard hall': 'Bozeman',
    'willow spring ranch': 'Belgrade',
    'old main gallery': 'Bozeman',
    'flower barn at rathvinden farm': 'Belgrade',
}

SKIP_LINES = re.compile(
    r'^(?:tickets?|free (?:concert|admission)|admission|\$|donations?|'
    r'limited seating|please note|\*{2,}|change in venue)',
    re.IGNORECASE,
)
NON_VENUE_LINES = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|'
    rf'(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+20\d{{2}})?$|'
    r'@?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|noon)$|'
    r'Faculty Concert\b|This is not our usual Friday evening concert$)',
    re.IGNORECASE,
)


def make_session():
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


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def page_year(soup):
    heading = soup.select_one('h1')
    date_text = heading.find_next(string=re.compile(r'\b20\d{2}\b')) if heading else None
    match = re.search(r'\b(20\d{2})\b', date_text or '')
    return int(match.group(1)) if match else None


def parse_date_time(match, fallback_year):
    year = int(match.group('year') or fallback_year or 0)
    if not year:
        return None
    try:
        event_date = datetime.strptime(
            f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None

    hour = int(match.group('hour'))
    minute = int(match.group('minute') or 0)
    meridiem = match.group('meridiem').lower()
    if meridiem == 'noon':
        hour = 12
    else:
        hour = hour % 12 + (12 if meridiem == 'pm' else 0)
    if hour > 23 or minute > 59:
        return None
    return event_date, f'{hour:02d}:{minute:02d}'


def city_from_text(value):
    normalized = value.casefold()
    for city in CITY_NAMES:
        if re.search(rf'\b{re.escape(city.casefold())}\b', normalized):
            return city
    return None


def looks_like_address(value):
    return bool(re.match(r'^\d+\b', value)) or bool(
        re.search(r'\b(?:street|ave|avenue|rd|road|blvd|broadway|way|lane)\b', value, re.I)
    )


def venue_from_lines(lines, occurrence_node):
    useful = []
    for line in lines:
        line = clean_text(line).strip(' |')
        if (
            not line
            or OCCURRENCE_RE.search(line)
            or SKIP_LINES.search(line)
            or NON_VENUE_LINES.search(line)
        ):
            continue
        if looks_like_address(line):
            continue
        # A combined venue/address line uses its first comma-separated field.
        candidate = line.split(',', 1)[0].strip()
        if candidate and candidate.casefold() not in {city.casefold() for city in CITY_NAMES}:
            useful.append(candidate)
    if useful:
        return useful[0]

    column = occurrence_node.find_parent(class_=re.compile(r'\bet_pb_column\b'))
    if column:
        prior = []
        for text in column.stripped_strings:
            if text == next(iter(occurrence_node.stripped_strings), None):
                break
            value = clean_text(text)
            if value and not SKIP_LINES.search(value):
                prior.append(value)
        for value in reversed(prior):
            if value.casefold() in VENUE_CITIES:
                return value
    return None


def parse_detail(post, response_text):
    soup = BeautifulSoup(response_text, 'html.parser')
    title = clean_text(BeautifulSoup(
        html.unescape(post.get('title', {}).get('rendered', '')), 'html.parser'
    ).get_text(' ', strip=True))
    url = clean_text(post.get('link'))
    content = soup.select_one('#main-content')
    description = clean_text(content.get_text('\n', strip=True) if content else '') or None
    fallback_year = page_year(soup)
    if not title or not url or not fallback_year:
        return []

    records = []
    for paragraph in soup.select('#main-content p'):
        lines = [clean_text(value) for value in paragraph.stripped_strings]
        combined = ' '.join(lines)
        match = OCCURRENCE_RE.search(combined)
        if not match:
            continue
        parsed = parse_date_time(match, fallback_year)
        venue = venue_from_lines(lines, paragraph)
        context = ' '.join(lines)
        city = city_from_text(context)
        if venue and not city:
            city = VENUE_CITIES.get(venue.casefold())
        if not parsed or not venue or not city:
            log_message(
                'Skipping concert occurrence without a usable date, venue, or city',
                event='crawler_event_skipped',
                level='warning',
                url=url,
            )
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class MontanaChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='montanachambermusic_org',
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

    def fetch_posts(self, session):
        posts = []
        page = 1
        while True:
            response = session.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    '_fields': 'id,link,title',
                },
                timeout=45,
            )
            response.raise_for_status()
            posts.extend(response.json())
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                return posts
            page += 1

    def scrape(self):
        session = make_session()
        try:
            posts = self.fetch_posts(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Montana Chamber Music concert feed',
                event='crawler_listing_request_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for post in posts:
            url = post.get('link', '')
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_detail(post, response.text))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Montana Chamber Music concert detail',
                    event='crawler_detail_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    MontanaChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
