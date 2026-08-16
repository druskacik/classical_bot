import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fvso.org/'
SOURCE = 'Farmington Valley Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

DATE_RE = re.compile(
    r'\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'([A-Z][a-z]+),?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.I)
SEASON_RE = re.compile(r'^(\d{4})-(\d{4})(?:-season)?$')

CITY_NAMES = (
    'West Hartford', 'East Hartford', 'New Britain', 'Newington', 'Collinsville',
    'Farmington', 'Hartford', 'Simsbury', 'Avon', 'Canton', 'Bristol', 'Windsor',
)

VENUE_CITY = {
    'Hoffman Auditorium': 'West Hartford',
    'University of Saint Joseph': 'West Hartford',
    'University of St. Joseph': 'West Hartford',
    'Northwest Catholic': 'West Hartford',
    "St. John's Episcopal": 'West Hartford',
    'Christ Church Cathedral': 'Hartford',
    'First Church': 'Farmington',
    'Farmington High School': 'Farmington',
    'Porter Memorial': 'Farmington',
    'Lincoln Theater': 'Hartford',
    'Millard Auditorium': 'Hartford',
    'Belding Theater': 'Hartford',
    'Westminster School': 'Simsbury',
    'ENO Memorial Hall': 'Simsbury',
    'Central Connecticut State University': 'New Britain',
    'Welte Hall': 'New Britain',
    'Mandell Jewish Community Center': 'West Hartford',
    'Mandell JCC': 'West Hartford',
    'Saint Joseph College': 'West Hartford',
    'Miss Porter': 'Farmington',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value or '')
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    concert_match = re.search(r'\bconcert\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', value or '', re.I)
    if concert_match:
        match = concert_match
    else:
        match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute, period = match.groups()
    hour = int(hour) % 12 + (12 if period.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def infer_city(value):
    for city in CITY_NAMES:
        if re.search(rf'\b{re.escape(city)}\b', value, re.I):
            return city
    for venue, city in VENUE_CITY.items():
        if venue.lower() in value.lower():
            return city
    return None


def venue_from_lines(lines, date_index):
    date_line = lines[date_index]
    same_line = DATE_RE.sub('', date_line, count=1)
    same_line = re.sub(r'^\s*(?:at\s+)?\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?,?\s*', '', same_line, flags=re.I)
    candidates = [same_line.strip(' ,-')] + lines[date_index + 1:date_index + 6]
    rejected = re.compile(
        r'\b(?:conductor|director|soloist|tickets?|sponsored|intermission|picnick|doors? open)\b',
        re.I,
    )
    for line in candidates:
        if not line or rejected.search(line) or TIME_RE.search(line):
            continue
        if infer_city(line) or re.search(
            r'\b(?:auditorium|cathedral|church|school|university|lawn|grounds|hall|center|theatre|park)\b',
            line,
            re.I,
        ):
            return re.sub(r',?\s*\d{1,5}\s+.*$', '', line).strip(' ,-')
    return None


def make_record(title, date_text, url, lines, description):
    event_date = parse_date(date_text)
    if not title or not event_date:
        return None
    try:
        date_index = next(index for index, line in enumerate(lines) if DATE_RE.search(line))
    except StopIteration:
        return None
    venue = venue_from_lines(lines, date_index)
    city = infer_city('\n'.join(lines[date_index:date_index + 7]))
    if not venue or not city:
        return None
    return {
        'title': title.strip(),
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_text),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_detail_page(page, require_single_date=False):
    soup = BeautifulSoup(page.get('content', {}).get('rendered', ''), 'html.parser')
    lines = [line for line in clean_text(soup).splitlines() if line]
    date_lines = [line for line in lines if DATE_RE.search(line)]
    if not date_lines or (require_single_date and len(date_lines) != 1):
        return None
    title = clean_text(page.get('title', {}).get('rendered', ''))
    return make_record(title, date_lines[0], page['link'], lines, '\n'.join(lines))


def parse_archive_page(page):
    soup = BeautifulSoup(page.get('content', {}).get('rendered', ''), 'html.parser')
    url = page['link']
    records = []
    anchors = [anchor for anchor in soup.find_all('a', attrs={'name': True}) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', anchor['name'])]

    for anchor in anchors:
        event_container = anchor.find_parent(['p', 'td'])
        if not event_container:
            continue
        lines = [line for line in clean_text(event_container).splitlines() if line]
        date_line = next((line for line in lines if DATE_RE.search(line)), '')
        date_index = next((index for index, line in enumerate(lines) if DATE_RE.search(line)), 0)
        title_lines = [line for line in lines[:date_index] if line]
        title = title_lines[0] if title_lines else f'FVSO Concert – {anchor["name"]}'

        description_parts = [clean_text(event_container)]
        sibling = event_container.find_next_sibling()
        while sibling and not sibling.find('a', attrs={'name': re.compile(r'^\d{4}-\d{2}-\d{2}$')}):
            text = clean_text(sibling)
            if text:
                description_parts.append(text)
            sibling = sibling.find_next_sibling()
        description = '\n'.join(description_parts)
        event_url = f"{url}#{anchor['name']}"
        record = make_record(title, date_line, event_url, lines, description)
        if record:
            records.append(record)
    return records


def fetch_pages(session):
    response = session.get(
        API_URL,
        params={
            'per_page': 100,
            'page': 1,
            '_fields': 'id,link,slug,title,parent,content',
        },
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    pages = fetch_pages(session)
    by_id = {page['id']: page for page in pages}
    archive_parent_ids = {
        page['id'] for page in pages if page.get('slug') == 'program-archives'
    }
    archived_seasons = set()
    records = []

    for page in pages:
        if page.get('parent') not in archive_parent_ids or not SEASON_RE.match(page.get('slug', '')):
            continue
        match = SEASON_RE.match(page['slug'])
        archived_seasons.add(int(match.group(1)))
        records.extend(parse_archive_page(page))

    for page in pages:
        parent = by_id.get(page.get('parent'))
        parent_match = SEASON_RE.match(parent.get('slug', '')) if parent else None
        if parent_match and int(parent_match.group(1)) in archived_seasons:
            continue
        if parent_match:
            record = parse_detail_page(page)
            if record:
                records.append(record)
            continue
        if page.get('parent') == 0 and not SEASON_RE.match(page.get('slug', '')):
            record = parse_detail_page(page, require_single_date=True)
            if record:
                records.append(record)

    unique = {}
    for record in records:
        key = (record['title'].casefold(), record['date'], record['time_from'], record['venue'].casefold())
        unique[key] = record

    result = sorted(unique.values(), key=lambda item: (item['date'], item['title']))
    if not result:
        log_message(
            'No FVSO concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return result


class FvsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fvso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    FvsoOrgCrawler().run()


if __name__ == '__main__':
    main()
