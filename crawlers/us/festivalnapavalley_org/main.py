import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://festivalnapavalley.org/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'about-us/past-events-listing/')
SOURCE = 'Festival Napa Valley'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'[A-Za-z]+\s+\d{1,2},\s+\d{4}')
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[AP]M)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    compact = re.sub(r'\s+', ' ', match.group(1).upper())
    compact = re.sub(r'(?<=\d)(AM|PM)$', r' \1', compact)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(compact, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def archive_years(session):
    response = session.get(ARCHIVE_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    years = {
        int(match.group(1))
        for link in soup.select('a[href*="year="]')
        if (match := re.search(r'[?&]year=(\d{4})', link.get('href', '')))
    }
    current_year = datetime.now().year
    years.add(current_year)
    return sorted(year for year in years if 2000 <= year <= current_year)


def listing_items(session, year):
    response = session.get(ARCHIVE_URL, params={'year': year}, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    items = []
    for row in soup.select('.itemRow'):
        link = row.select_one('dd.readmore a[href], .readMore a[href]')
        title_node = row.select_one('.listContent h2, .listContent h3, .listTitle')
        date_node = row.select_one('.listDate, time')
        if not link:
            continue
        url = urljoin(ARCHIVE_URL, link.get('href'))
        event_date = parse_date(date_node.get_text(' ', strip=True) if date_node else row.get_text(' ', strip=True))
        title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
        if not title:
            headings = row.select('.listContent h1, .listContent h2, .listContent h3, dt')
            title = clean_text(headings[-1].get_text(' ', strip=True)) if headings else ''
        if title and event_date and url.startswith(SOURCE_URL):
            items.append({'title': title, 'date': event_date, 'url': url})
    return items


def content_container(soup):
    title = soup.select_one('h1.h2, h1')
    if not title:
        return None
    node = title.parent
    while node and not node.select_one('.pageTitle'):
        node = node.parent
    return node or title.parent


def detail_record(item, session):
    response = session.get(item['url'], timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title_node = soup.select_one('h1.h2, h1')
    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else item['title']
    meta = soup.select_one('.pageTitle')
    meta_lines = [clean_text(part) for part in meta.stripped_strings] if meta else []
    meta_lines = [line for line in meta_lines if line and line != '|']

    venue = meta_lines[1] if len(meta_lines) > 1 else ''
    location_line = meta_lines[2] if len(meta_lines) > 2 else ''
    city = clean_text(location_line.rsplit('|', 1)[-1]) if location_line else ''
    if city and re.search(r'\d', city):
        city = ''

    container = content_container(soup)
    description_parts = []
    if container:
        for paragraph in container.find_all('p'):
            if paragraph is meta or 'pageTitle' in (paragraph.get('class') or []):
                continue
            text = clean_text(paragraph.get_text(' ', strip=True))
            if text.lower() in {'performers', 'venue'}:
                break
            if text and text.lower() not in {'learn more >', 'details'}:
                description_parts.append(text)

    if not title or not venue or not city:
        return None
    return {
        'title': title,
        'date': item['date'],
        'url': item['url'],
        'time_from': parse_time(meta.get_text(' ', strip=True) if meta else ''),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(dict.fromkeys(description_parts)) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    years = archive_years(session)
    items = []
    for year in years:
        items.extend(listing_items(session, year))

    unique_items = {item['url']: item for item in items}
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_record, item, session): item['url']
            for item in unique_items.values()
        }
        for future in as_completed(futures):
            try:
                record = future.result()
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=futures[future],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=ARCHIVE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class FestivalNapaValleyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalnapavalley_org',
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
    FestivalNapaValleyOrgCrawler().run()


if __name__ == '__main__':
    main()
