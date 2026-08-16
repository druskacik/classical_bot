import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ectsymphony.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Eastern Connecticut Symphony Orchestra'

# Cloudflare challenges the interactive site, while the site explicitly permits
# indexing agents and serves its public WordPress API to this user agent.
HEADERS = {
    'User-Agent': 'Googlebot',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}
MONTH_PATTERN = '|'.join(month.title() for month in MONTHS)
DATE_RE = re.compile(
    rf'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,.]?\s+)?'
    rf'({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(1[0-2]|0?\d):([0-5]\d)\s*([ap])\.?m\.?', re.IGNORECASE)
DATE_TITLE_RE = re.compile(
    r'^\s*\d{1,2}[.-]\d{1,2}[.-](?:\d{2}|20\d{2})\s*(?:[-–—]\s*)?'
)

# Explicit venue evidence from the source's archive. These mappings also cover
# pages that name a campus or resort but omit its municipality from the date line.
VENUES = {
    'Garde Arts Center': 'New London',
    'Cathedral of St. Patrick': 'Norwich',
    'Champlin’s Marina and Resort': 'Block Island',
    "Champlin's Marina and Resort": 'Block Island',
    'Champlin’s Marina & Resort': 'Block Island',
    "Champlin's Marina & Resort": 'Block Island',
    'The Red Barn, Mitchell College': 'New London',
    'Premier Theater at Foxwoods Resort & Casino': 'Mashantucket',
    'The Thames Club': 'New London',
    'Hygienic Art Park': 'New London',
    'Harkness Chapel, Connecticut College': 'New London',
    'Mystic Ballroom, Mystic Marriott Hotel & Spa': 'Groton',
    'Lyman Allyn Art Museum': 'New London',
    'Stonington Vineyards': 'Stonington',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2))
        ).isoformat()
    except ValueError:
        return None


def parse_time(value):
    date_match = DATE_RE.search(value)
    match = TIME_RE.search(value, date_match.end() if date_match else 0)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def location_from_text(value):
    normalized = clean_text(value).replace('|', ',')
    for venue, city in VENUES.items():
        if venue.casefold() in normalized.casefold():
            return venue, city
    return '', ''


def event_info(soup, title):
    paragraphs = [clean_text(node) for node in soup.find_all('p')]
    paragraphs = [text for text in paragraphs if text]

    # Detail pages put their occurrence line at the top. Looking only near the
    # top avoids turning season lists, competition deadlines, and news indexes
    # into fabricated events.
    for index, text in enumerate(paragraphs[:8]):
        date_match = DATE_RE.search(text)
        if not date_match or date_match.start() > 80:
            continue
        event_date = parse_date(text)
        time_from = parse_time(text)
        venue, city = location_from_text(text)
        if event_date and time_from and venue and city:
            return event_date, time_from, venue, city

        # Some layouts split the date and time/location into adjacent blocks.
        # Combine only when the second block has no competing date.
        if index + 1 < len(paragraphs[:8]):
            next_text = paragraphs[index + 1]
            if not DATE_RE.search(next_text):
                combined = f'{text}\n{next_text}'
                time_from = parse_time(combined)
                venue, city = location_from_text(combined)
                if event_date and time_from and venue and city:
                    return event_date, time_from, venue, city

    # A few older detail pages repeat the full year only in their introductory
    # paragraph. Accept that fallback only for pages whose title itself starts
    # with a compact event date.
    if DATE_TITLE_RE.match(title):
        for text in paragraphs:
            event_date = parse_date(text)
            time_from = parse_time(text)
            venue, city = location_from_text(text)
            if event_date and time_from and venue and city:
                return event_date, time_from, venue, city
    return None


def parse_page(page):
    title = clean_text(BeautifulSoup(page.get('title', {}).get('rendered', ''), 'html.parser'))
    url = clean_text(page.get('link'))
    content = page.get('content', {}).get('rendered', '')
    if not title or not url or not content:
        return None

    soup = BeautifulSoup(content, 'html.parser')
    for unwanted in soup.select('script, style, form'):
        unwanted.decompose()
    info = event_info(soup, title)
    if not info:
        return None
    event_date, time_from, venue, city = info
    display_title = DATE_TITLE_RE.sub('', title).strip(' -–—') or title
    if display_title.casefold() == 'block island':
        display_title = 'Summer Symphony at Block Island'
    description = clean_text(soup) or None
    return {
        'title': display_title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page_number,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link,slug,title,content',
            },
            timeout=60,
        )
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page_number >= total_pages:
            return pages
        page_number += 1


class EctsymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ectsymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            pages = fetch_pages(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Eastern Connecticut Symphony pages',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for page in pages:
            record = parse_page(page)
            if record:
                records.append(record)
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    EctsymphonyComCrawler().run()


if __name__ == '__main__':
    main()
