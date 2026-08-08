import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tafelmusik.org/'
SOURCE = 'Tafelmusik'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concert'
CITY = 'Toronto'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-CA,en;q=0.9',
}

DATE_PATTERN = re.compile(
    r'(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2})(?:,\s*(?P<year>20\d{2}))?'
    r'\s*,?\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>[ap]m)',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = unescape(text).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value, fallback_year=None):
    match = DATE_PATTERN.search(clean_text(value))
    if not match:
        return None
    year = match.group('year') or (str(fallback_year) if fallback_year else None)
    if not year:
        return None
    minute = match.group('minute') or '00'
    raw = (
        f"{match.group('month')} {match.group('day')}, {year} "
        f"{match.group('hour')}:{minute} {match.group('period')}"
    )
    try:
        parsed = datetime.strptime(raw, '%B %d, %Y %I:%M %p')
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def valid_venue(value):
    venue = clean_text(value).strip(' ,–-')
    if not venue or venue.lower() in {'multiple venues', 'date/time tba'}:
        return None
    if 'date/time tba' in venue.lower():
        return None
    return venue


def description_from(soup, excerpt):
    content = BeautifulSoup(str(soup), 'html.parser')
    for selector in (
        '.page-header', '.ticket-information', '.ticket-footer', '.box-office',
        '.actions', 'script', 'style', 'figure', 'noscript',
    ):
        for node in content.select(selector):
            node.decompose()
    description = clean_text(content)
    return description or clean_text(excerpt) or None


def standard_performances(soup):
    performances = []
    for group in soup.select('.performance-set'):
        heading = group.select_one('h4')
        venue = valid_venue(heading)
        if not venue:
            continue
        for node in group.select('.performance .date'):
            parsed = parse_datetime(node)
            if parsed:
                performances.append((parsed[0], parsed[1], venue))
    return performances


def adjacent_footer_performances(soup):
    """Parse older festival pages whose date is in a group's following footer."""
    performances = []
    for group in soup.select('.performance-set'):
        venue = valid_venue(group.select_one('h4'))
        if not venue or group.select('.performance .date'):
            continue
        sibling = group.find_next_sibling()
        while sibling is not None and 'performance-set' not in (sibling.get('class') or []):
            if 'ticket-footer' in (sibling.get('class') or []):
                for node in sibling.select('p'):
                    parsed = parse_datetime(node)
                    if parsed:
                        performances.append((parsed[0], parsed[1], venue))
                break
            sibling = sibling.find_next_sibling()
    return performances


def multi_venue_performances(soup, fallback_year):
    """Parse the current festival layout, which stores each show in a footer paragraph."""
    performances = []
    for group in soup.select('.performance-set'):
        if clean_text(group.select_one('h4')).lower() != 'multiple venues':
            continue
        footer = group.find_next(class_='ticket-footer')
        if footer is None:
            continue
        for paragraph in footer.select(':scope > p'):
            parsed = parse_datetime(paragraph, fallback_year=fallback_year)
            if not parsed:
                continue
            lines = [clean_text(line) for line in paragraph.decode_contents().split('<br/>')]
            lines = [line for line in lines if line]
            venue = None
            for line in lines:
                if parse_datetime(line, fallback_year=fallback_year):
                    continue
                if any(term in line.lower() for term in (
                    'hall', 'church', 'centre', 'center', 'theatre', 'theater', 'auditorium'
                )):
                    venue = valid_venue(line)
                    break
            if venue:
                performances.append((parsed[0], parsed[1], venue))
    return performances


def records_from_item(item):
    title = clean_text((item.get('title') or {}).get('rendered'))
    url = item.get('link') or ''
    html = (item.get('content') or {}).get('rendered') or ''
    if not title or not url or not html:
        return []

    soup = BeautifulSoup(html, 'html.parser')
    performances = standard_performances(soup)
    performances.extend(adjacent_footer_performances(soup))
    try:
        fallback_year = datetime.fromisoformat(item.get('date', '')).year
    except (TypeError, ValueError):
        fallback_year = None
    performances.extend(multi_venue_performances(soup, fallback_year))

    description = description_from(soup, (item.get('excerpt') or {}).get('rendered'))
    records = []
    for event_date, time_from, venue in performances:
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'CA',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_concerts(session):
    items = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'desc'},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        items.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return items
        page += 1


class TafelmusikOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tafelmusik_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
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
            items = fetch_concerts(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Tafelmusik concerts',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for item in items:
            records.extend(records_from_item(item))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    TafelmusikOrgCrawler().run()


if __name__ == '__main__':
    main()
