import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nycpmusic.org/'
SOURCE = 'New York Classical Players'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'

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
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})
MONTH_PATTERN = '|'.join(sorted(MONTHS, key=len, reverse=True))


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            PAGES_API,
            params={'per_page': 100, 'page': page_number, 'context': 'view'},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page_number >= total_pages:
            return pages
        page_number += 1


def title_dates(title):
    """Return ordered (month, day) pairs from the title's date suffix."""
    match = re.search(r'\(([^()]*)\)\s*$', title)
    if not match:
        return []
    value = match.group(1)
    month_matches = list(re.finditer(rf'\b({MONTH_PATTERN})\b', value, re.I))
    result = []
    for index, month_match in enumerate(month_matches):
        end = month_matches[index + 1].start() if index + 1 < len(month_matches) else len(value)
        segment = value[month_match.end():end]
        numbers = [int(number) for number in re.findall(r'\d{1,2}', segment)]
        if '-' in segment and len(numbers) == 2:
            numbers = list(range(numbers[0], numbers[1] + 1))
        result.extend((MONTHS[month_match.group(1).lower()], day) for day in numbers)
    return result


def season_years(pages):
    years = {}
    for page in pages:
        title = clean_text(page.get('title', {}).get('rendered'))
        match = re.fullmatch(r'(20\d{2})[-–]\d{2} Season', title, re.I)
        if not match:
            continue
        start_year = int(match.group(1))
        soup = BeautifulSoup(page.get('content', {}).get('rendered', ''), 'html.parser')
        for link in soup.select('a[href]'):
            path = requests.utils.urlparse(link.get('href', '')).path.strip('/')
            if path:
                years[path] = start_year
    return years


def event_year(page, month, linked_seasons):
    slug = page.get('slug', '')
    numeric = re.match(r'^(\d{2})(\d{2})', slug)
    if numeric and int(numeric.group(2)) == month:
        return 2000 + int(numeric.group(1))

    if slug in linked_seasons:
        start_year = linked_seasons[slug]
        return start_year if month >= 7 else start_year + 1

    # Standalone special events are normally published shortly before their
    # occurrence. This also covers the site's family-event pages.
    published = date.fromisoformat(page['date'][:10])
    return published.year if month >= published.month else published.year + 1


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def block_location(block):
    lines = [line for line in clean_text(block).splitlines() if line]
    if len(lines) < 3:
        return None
    address = lines[-1]
    city_match = re.search(r',\s*([^,]+),\s*[A-Z]{2}(?:\s+\d{5})?\s*$', address, re.I)
    if not city_match:
        return None
    city = city_match.group(1).strip()
    city = {
        'new york': 'New York City',
        'brooklyn': 'Brooklyn',
        'fort lee': 'Fort Lee',
    }.get(city.lower(), city)
    venue = lines[-2].strip(' ,')
    if not venue or re.fullmatch(rf'(?:{MONTH_PATTERN})?\s*\d{{1,2}}.*', venue, re.I):
        return None
    return venue, city


def page_description(soup):
    copy = BeautifulSoup(str(soup), 'html.parser')
    for element in copy.select('.wp-block-buttons'):
        element.decompose()
    for heading in copy.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        if clean_text(heading).lower() in {'ticket', 'tickets'}:
            heading.decompose()
    return clean_text(copy) or None


def parse_page(page, linked_seasons):
    soup = BeautifulSoup(page.get('content', {}).get('rendered', ''), 'html.parser')
    blocks = [
        block for block in soup.select('.wp-block-button')
        if re.search(rf'\b(?:{MONTH_PATTERN})\b\s*\d', clean_text(block), re.I)
    ]
    if not blocks:
        return []

    title = clean_text(page.get('title', {}).get('rendered'))
    dates = title_dates(title)
    if len(dates) != len(blocks):
        dates = []
        for block in blocks:
            match = re.search(rf'\b({MONTH_PATTERN})\b\s*(\d{{1,2}})', clean_text(block), re.I)
            if not match:
                return []
            dates.append((MONTHS[match.group(1).lower()], int(match.group(2))))

    description = page_description(soup)
    records = []
    for block, (month, day) in zip(blocks, dates):
        location = block_location(block)
        if not location:
            continue
        year = event_year(page, month, linked_seasons)
        try:
            event_date = date(year, month, day).isoformat()
        except ValueError:
            continue
        venue, city = location
        records.append({
            'title': re.sub(r'\s*\([^()]*\)\s*$', '', title).strip(),
            'date': event_date,
            'url': page.get('link', ''),
            'time_from': parse_time(clean_text(block)),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class NycpMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nycpmusic_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            pages = get_pages(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch New York Classical Players pages',
                event='crawler_fetch_failed',
                level='error',
                url=PAGES_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        linked_seasons = season_years(pages)
        records = []
        for page in pages:
            records.extend(parse_page(page, linked_seasons))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    NycpMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
