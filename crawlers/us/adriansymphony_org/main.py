import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.adriansymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-list.html')
SUMMER_URL = urljoin(SOURCE_URL, 'summer-concerts.html')
SOURCE = 'Adrian Symphony Orchestra'
CITY = 'Adrian'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?\s*'
    r'(?:at|\|)\s*(\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value, default_year=None):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year, time_value = match.groups()
    year = year or default_year
    if not year:
        return None
    try:
        date_value = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date()
    except ValueError:
        try:
            date_value = datetime.strptime(f'{month} {day} {year}', '%b %d %Y').date()
        except ValueError:
            return None
    time_value = re.sub(r'(?<=\d)(?=[AP]M$)', ' ', time_value.upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            time_from = datetime.strptime(time_value.upper(), pattern).strftime('%H:%M')
            return date_value.isoformat(), time_from
        except ValueError:
            pass
    return None


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def season_year(soup):
    heading = soup.find(['h1', 'h2'], string=re.compile(r'\b20\d{2}-20\d{2}\b'))
    if not heading:
        return None
    match = re.search(r'\b(20\d{2})-(20\d{2})\b', clean_text(heading))
    return int(match.group(1)) if match else None


def event_year(month, season_start):
    if not season_start:
        return None
    try:
        month_number = datetime.strptime(month[:3], '%b').month
    except ValueError:
        return None
    return season_start if month_number >= 7 else season_start + 1


def detail_description(session, url):
    try:
        soup = get_soup(session, url)
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    main = soup.find('main') or soup.select_one('.main-content') or soup.body
    if not main:
        return None
    for node in main.select(
        'script, style, nav, form, .breadcrumb, .ticket-prices, '
        '.button-container, footer, .footer'
    ):
        node.decompose()
    text = clean_text(main)
    return text or None


def listing_records(session, listing_url):
    soup = get_soup(session, listing_url)
    start_year = season_year(soup)
    records = []
    for card in soup.select('.exhibition-teaser'):
        title_node = card.find(['h2', 'h3', 'h4'])
        date_node = card.select_one('.date')
        venue_node = card.select_one('.location')
        detail_link = (
            card.select_one('.button-container a[href]')
            or card.select_one('.hero-image a[href]')
            or title_node.find('a', href=True)
        )
        if not all((title_node, date_node, venue_node, detail_link)):
            continue

        date_text = clean_text(date_node)
        month_match = re.search(r'\b([A-Za-z]+)\s+\d{1,2}\b', date_text)
        default_year = event_year(month_match.group(1), start_year) if month_match else None
        parsed = parse_date_time(date_text, default_year)
        title = clean_text(title_node)
        venue = clean_text(venue_node)
        url = urljoin(listing_url, detail_link.get('href'))
        if not parsed or not title or not venue or not url.startswith(('http://', 'https://')):
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records, soup


def summer_records(session):
    soup = get_soup(session, SUMMER_URL)
    heading = soup.find(['h1', 'h2'], string=re.compile(r'20\d{2}\s+Summer Chamber Series', re.I))
    year_match = re.search(r'20\d{2}', clean_text(heading)) if heading else None
    venue_link = soup.find('a', href=re.compile(r'holy-rosary', re.I))
    venue = clean_text(venue_link) if venue_link else 'Holy Rosary Chapel'
    main = soup.find('main') or soup.body
    if not year_match or not main or not venue:
        return []

    records = []
    for title_node in main.select('.concert-header'):
        card = title_node.parent
        date_node = card.select_one('.date')
        title = clean_text(title_node)
        parsed = parse_date_time(date_node, int(year_match.group()))
        if not parsed or not title:
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': SUMMER_URL,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': clean_text(card) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records, current_soup = listing_records(session, LISTING_URL)

    archive_urls = []
    for link in current_soup.find_all('a', href=True):
        if re.search(r'20\d{2}-20\d{2}\s+Season Recap', clean_text(link), re.I):
            archive_urls.append(urljoin(LISTING_URL, link.get('href')))
    for archive_url in dict.fromkeys(archive_urls):
        try:
            archive_records, _ = listing_records(session, archive_url)
            records.extend(archive_records)
        except requests.RequestException as error:
            log_message(
                'Season archive request failed',
                event='crawler_archive_failed',
                level='warning',
                url=archive_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    try:
        records.extend(summer_records(session))
    except requests.RequestException as error:
        log_message(
            'Summer series request failed',
            event='crawler_summer_failed',
            level='warning',
            url=SUMMER_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    detail_urls = {record['url'] for record in unique.values() if record['url'] != SUMMER_URL}

    def fetch_description(url):
        detail_session = requests.Session()
        detail_session.headers.update(HEADERS)
        return url, detail_description(detail_session, url)

    with ThreadPoolExecutor(max_workers=6) as executor:
        descriptions = dict(executor.map(fetch_description, detail_urls))
    for record in unique.values():
        if record['url'] in descriptions:
            record['description'] = descriptions[record['url']]
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))
    if not result:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return result


class AdrianSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='adriansymphony_org',
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
        return scrape_concerts()


def main():
    AdrianSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
