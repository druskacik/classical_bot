import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://austinopera.org/'
SOURCE = 'Austin Opera'
CITY = 'Austin'
EVENTS_URL = urljoin(SOURCE_URL, 'shows-events/events/')

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
DATE_GROUP_RE = re.compile(
    rf'\b({MONTHS})\s+((?:\d{{1,2}}(?:\s*(?:,|&|and|–|-)\s*)?)+),?\s+(20\d{{2}})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def parse_dates(value):
    results = []
    for match in DATE_GROUP_RE.finditer(clean_text(value)):
        month, day_group, year = match.groups()
        for day in re.findall(r'\d{1,2}', day_group):
            try:
                parsed = datetime.strptime(f'{month} {day} {year}', '%B %d %Y')
            except ValueError:
                continue
            results.append(parsed.date().isoformat())
    return list(dict.fromkeys(results))


def page_links(soup, prefix):
    links = []
    for link in soup.select('main a[href]'):
        url = urljoin(SOURCE_URL, link.get('href')).split('#', 1)[0]
        path = urlparse(url).path.rstrip('/') + '/'
        if url.startswith(SOURCE_URL) and path.startswith(prefix) and path != prefix:
            links.append(url)
    return list(dict.fromkeys(links))


def season_landing_urls(soup):
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href')).split('?', 1)[0]
        if re.fullmatch(r'https://austinopera\.org/shows-events/20\d{2}-20\d{2}-season/', url):
            urls.append(url)

    # The navigation only retains the active season. Recent archived production
    # pages remain published, so probe a small rolling window as well.
    current_year = date.today().year
    season_start = current_year if date.today().month >= 7 else current_year - 1
    for start in range(season_start - 4, season_start + 2):
        urls.append(urljoin(SOURCE_URL, f'shows-events/{start}-{start + 1}-season/'))
    return list(dict.fromkeys(urls))


def extract_venue(lines, date_line_index):
    for line in lines:
        match = re.match(r'Location:\s*(.+)', line, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))

    date_line = lines[date_line_index]
    if '|' in date_line:
        candidate = clean_text(date_line.rsplit('|', 1)[1])
        if candidate and not parse_time(candidate):
            return candidate

    venue_words = re.compile(
        r'\b(Center|Centre|Theater|Theatre|Hall|Studio|Auditorium|Church|Club|Consulate)\b',
        re.IGNORECASE,
    )
    for line in lines[date_line_index + 1:date_line_index + 4]:
        candidate = clean_text(line)
        if (
            candidate
            and not parse_dates(candidate)
            and not parse_time(candidate)
            and len(candidate) <= 120
            and venue_words.search(candidate)
        ):
            return candidate
    return ''


def parse_detail_page(soup, url):
    main = soup.select_one('main')
    if not main:
        return []
    lines = [clean_text(line) for line in main.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    title = clean_text(soup.title.string) if soup.title else ''

    # Production dates are presented in the page masthead. Restricting this to
    # the beginning avoids mistaking dates in cast biographies or ancillary
    # pre-show promotions for the production itself on archived pages.
    date_line_index = next((i for i, line in enumerate(lines[:20]) if parse_dates(line)), None)
    if not title or date_line_index is None:
        return []
    dates = parse_dates(lines[date_line_index])
    venue = extract_venue(lines, date_line_index)
    if not dates or not venue:
        return []

    times_by_date = {}
    for link in main.select('a[href]'):
        label = clean_text(link.get_text(' ', strip=True))
        link_dates = parse_dates(label)
        link_time = parse_time(label)
        if link_time and not link_dates:
            partial = re.search(rf'\b({MONTHS})\s+(\d{{1,2}})\b', label, re.IGNORECASE)
            if partial:
                month_number = datetime.strptime(partial.group(1), '%B').month
                day_number = int(partial.group(2))
                link_dates = [
                    event_date for event_date in dates
                    if date.fromisoformat(event_date).month == month_number
                    and date.fromisoformat(event_date).day == day_number
                ]
        for event_date in link_dates:
            if link_time:
                times_by_date[event_date] = link_time

    description = clean_text(main.get_text('\n', strip=True)) or None
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': times_by_date.get(event_date),
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in dates]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    homepage_response = session.get(SOURCE_URL, timeout=45)
    homepage_response.raise_for_status()
    homepage = BeautifulSoup(homepage_response.text, 'html.parser')

    detail_urls = []
    for landing_url in season_landing_urls(homepage) + [EVENTS_URL]:
        try:
            response = session.get(landing_url, timeout=45)
            if response.status_code == 404:
                continue
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Austin Opera listing request failed',
                event='crawler_listing_request_failed',
                level='warning',
                url=landing_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        soup = BeautifulSoup(response.text, 'html.parser')
        prefix = urlparse(landing_url).path.rstrip('/') + '/'
        detail_urls.extend(page_links(soup, prefix))

    records = []
    for url in dict.fromkeys(detail_urls):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_detail_page(BeautifulSoup(response.text, 'html.parser'), url))
        except requests.RequestException as error:
            log_message(
                'Austin Opera detail request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No Austin Opera events found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class AustinOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='austinopera_org',
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
        return scrape_concerts()


def main():
    AustinOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
