import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operazuid.nl/'
AGENDA_API_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
PRODUCTIONS_API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/production')
SOURCE = 'Opera Zuid'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.request(url=url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def agenda_rows(session):
    """Load every page of Opera Zuid's first-party future agenda feed."""
    rows = []
    offset = None
    seen_offsets = set()
    while offset not in seen_offsets:
        seen_offsets.add(offset)
        data = {'action': 'oz_get_agendaitems', 'currentLang': 'nl'}
        if offset is not None:
            data['currentPostAmount'] = str(offset)
        response = get_response(
            session,
            AGENDA_API_URL,
            method='POST',
            data=data,
            headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': SOURCE_URL},
        )
        soup = BeautifulSoup(response.text, 'html.parser')
        page_rows = soup.select('.agendaitem-row')
        if not page_rows:
            break
        rows.extend(page_rows)
        load_more = soup.select_one('.load-more-agendaitems')
        if not load_more:
            break
        next_offset = len({parse_row_identity(row) for row in rows})
        if next_offset == offset:
            break
        offset = next_offset
    return rows


def parse_row_identity(row):
    return clean_text(row)


def resolve_geography(city):
    normalized = city.strip()
    if normalized.lower().startswith('online'):
        return None, None
    if normalized == 'Turnhout (BE)':
        return 'Turnhout', 'BE'
    if normalized == 'Brussel':
        return normalized, 'BE'
    if normalized == 'Keulen':
        return normalized, 'DE'
    normalized = re.sub(r'\s*\(theatraal concert\)\s*$', '', normalized, flags=re.I)
    return normalized, 'NL'


def parse_agenda_row(row):
    onclick = row.get('onclick', '')
    url_match = re.search(r"window\.location\.href=['\"]([^'\"]+)", onclick)
    columns = row.select(':scope > div')
    if not url_match or len(columns) < 3:
        return None

    title = clean_text(columns[0])
    date_time = clean_text(columns[1])
    location = [clean_text(part) for part in columns[2].stripped_strings]
    event_type = clean_text(columns[3]).lower() if len(columns) > 3 else ''
    match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4})(?:\s+(\d{1,2}:\d{2}))?', date_time)
    if not match or len(location) < 2 or 'besloten' in event_type:
        return None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None
    city, country_code = resolve_geography(location[0])
    venue = location[1]
    if not title or not city or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, url_match.group(1)),
        'time_from': match.group(2),
        'venue': venue,
        'city': city,
        'country_code': country_code,
    }


def production_description(session, url):
    soup = BeautifulSoup(get_response(session, url, method='GET').text, 'html.parser')
    parts = []
    composer = clean_text(soup.select_one('h2.composer'))
    if composer:
        parts.append(f'Componist\n{composer}')
    info = soup.select_one('.in-page-content .text')
    if info:
        text = clean_text(info)
        if text:
            parts.append(text)
    table_texts = []
    for table in soup.select('main table'):
        text = clean_text(table)
        if text and text not in table_texts:
            table_texts.append(text)
    if table_texts:
        parts.append('\n\n'.join(table_texts))
    return soup, '\n\n'.join(parts) or None


def production_links(session):
    links = set()
    page = 1
    total_pages = 1
    while page <= total_pages:
        response = get_response(
            session,
            PRODUCTIONS_API_URL,
            method='GET',
            params={'per_page': 100, 'page': page, '_fields': 'link'},
        )
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        links.update(item['link'] for item in response.json() if item.get('link'))
        page += 1
    return sorted(links)


def parse_detail_row(row, title, url):
    columns = row.select(':scope > div')
    if len(columns) < 2:
        return None
    date_time = clean_text(columns[0])
    location = [clean_text(part) for part in columns[1].stripped_strings]
    event_type = clean_text(columns[2]).lower() if len(columns) > 2 else ''
    match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4})(?:\s+(\d{1,2}:\d{2}))?', date_time)
    if not match or len(location) < 2 or 'besloten' in event_type:
        return None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None
    city, country_code = resolve_geography(location[0])
    venue = location[1]
    if not title or not city or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': match.group(2),
        'venue': venue,
        'city': city,
        'country_code': country_code,
    }


def scrape_production(session, url):
    soup, description = production_description(session, url)
    title = clean_text(soup.select_one('main h1'))
    records = []
    for row in soup.select('main .agendaitem-row'):
        record = parse_detail_row(row, title, url)
        if record:
            record['description'] = description
            records.append(record)
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    # The AJAX feed is the authoritative current agenda. The production REST
    # collection also exposes archived productions whose occurrence lists are
    # still published on their detail pages.
    parsed = [record for row in agenda_rows(session) if (record := parse_agenda_row(row))]
    urls = production_links(session)
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scrape_production, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                parsed.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    by_identity = {}
    for record in parsed:
        identity = (record['title'], record['date'], record['time_from'], record['venue'])
        record.setdefault('description', None)
        record['source_url'] = SOURCE_URL
        record['source'] = SOURCE
        existing = by_identity.get(identity)
        if not existing or (not existing['description'] and record['description']):
            by_identity[identity] = record
    records = list(by_identity.values())
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OperaZuidNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operazuid_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaZuidNlCrawler().run()


if __name__ == '__main__':
    main()
