import html
import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wisphil.org/'
SOURCE = 'Wisconsin Philharmonic'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})',
    re.I,
)
TIME_RE = re.compile(r'(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.I)
CITY_RE = re.compile(r',\s*([^,\n]+),\s*WI(?:\s+\d{5})?\b', re.I)

VENUE_CITIES = {
    'Oconomowoc Arts Center': 'Oconomowoc',
    'Sharon Lynne Wilson Center for the Arts': 'Brookfield',
    'Sharon Lynne Wilson Center': 'Brookfield',
    "St. Luke's Lutheran Church": 'Waukesha',
    'St. Luke’s Lutheran Church': 'Waukesha',
    'Shattuck Music Center': 'Waukesha',
    'Rustic Manor 1848': 'Hartland',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n *|\n{3,}', '\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def city_from_location(venue, address, description=''):
    match = CITY_RE.search(clean_text(address))
    if match:
        return match.group(1).strip()
    combined = clean_text(f'{venue}\n{description}')
    for known_venue, city in VENUE_CITIES.items():
        if known_venue.lower() in combined.lower():
            return city
    match = re.search(r'\b(Waukesha|Brookfield|Oconomowoc|Hartland)\b', combined, re.I)
    return match.group(1).title() if match else None


def get_json(session, path, params=None):
    response = session.get(f'{API_URL}/{path}', params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def event_schema(soup, url):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data.get('@graph', []) if isinstance(data, dict) else []
        if isinstance(data, dict):
            candidates = [data, *candidates]
        for item in candidates:
            if item.get('@type') == 'Event' and item.get('url', '').rstrip('/') == url.rstrip('/'):
                return item
    return None


def parse_mec_event(session, item):
    url = item.get('link', '')
    if not url:
        return None
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Event detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    schema = event_schema(soup, url)
    if not schema:
        return None
    location = schema.get('location') or {}
    venue = clean_text(location.get('name'))
    address = location.get('address') or ''
    if isinstance(address, dict):
        address = ', '.join(filter(None, [
            address.get('streetAddress'), address.get('addressLocality'),
            address.get('addressRegion'), address.get('postalCode'),
        ]))
    description_node = soup.select_one('.mec-single-event-description')
    description = clean_text(description_node) if description_node else clean_text(schema.get('description'))
    city = city_from_location(venue, address, description)
    event_date = str(schema.get('startDate', ''))[:10]
    try:
        datetime.strptime(event_date, '%Y-%m-%d')
    except ValueError:
        return None
    if not venue or not city:
        return None
    time_node = soup.select_one('.mec-single-event-time .mec-events-abbr')
    return {
        'title': clean_text(schema.get('name') or item.get('title', {}).get('rendered')),
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_node.get_text(' ', strip=True)) if time_node else parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def season_records(page):
    soup = BeautifulSoup(page.get('content', {}).get('rendered', ''), 'html.parser')
    records = []
    headings = soup.find_all(['h2', 'h3'])
    for heading in headings:
        title = clean_text(heading)
        if not title or re.search(r'\bseason\b|tickets|subscription', title, re.I):
            continue
        parts = []
        for node in heading.next_siblings:
            if getattr(node, 'name', None) in {'h2', 'h3'} and clean_text(node):
                break
            text = clean_text(node)
            if text:
                parts.append(text)
        block = '\n'.join(parts)
        event_date = parse_date(block)
        if not event_date:
            continue
        lines = [line for line in block.splitlines() if line.strip()]
        date_index = next((index for index, line in enumerate(lines) if parse_date(line)), None)
        after_date = lines[date_index + 1:date_index + 6] if date_index is not None else []
        venue = next(
            (
                line.strip() for line in after_date
                if any(known.lower() in line.lower() for known in VENUE_CITIES)
                or re.search(r'\b(?:Arts Center|Music Center|Church|Manor|Bandshell)\b', line, re.I)
            ),
            '',
        )
        venue = re.sub(r'^(?:at\s+)', '', venue, flags=re.I).strip()
        if re.search(r'activities|admission|soloist|guest|doors|tickets|\$', venue, re.I):
            venue = ''
        city = city_from_location(venue, '', block)
        if not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': page['link'],
            'time_from': parse_time(lines[date_index]) if date_index is not None else parse_time(block),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': block or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    items = get_json(session, 'mec-events', {'per_page': 100, 'page': 1})
    records = [record for item in items if (record := parse_mec_event(session, item))]

    pages = get_json(
        session,
        'pages',
        {'search': 'season', 'per_page': 100, '_fields': 'id,slug,link,title,content'},
    )
    for page in pages:
        if re.fullmatch(r'20\d{2}-20\d{2}-season', page.get('slug', '')):
            records.extend(season_records(page))

    unique = {}
    for record in records:
        key = (record['title'].casefold(), record['date'], record['venue'].casefold())
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title']))
    if not result:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class WisphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wisphil_org',
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
    WisphilOrgCrawler().run()


if __name__ == '__main__':
    main()
