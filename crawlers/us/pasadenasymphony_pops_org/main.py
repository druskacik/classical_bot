import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pasadenasymphony-pops.org/'
SITEMAP_URL = f'{SOURCE_URL}tribe_events-sitemap.xml'
SOURCE = 'Pasadena Symphony & Pops'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_LINE_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:\s*(?:&|and|-)\s*(\d{1,2}))?,\s*(\d{4})$',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?', re.IGNORECASE)
VENUE_CITY_DEFAULTS = {
    'All Saints Church': 'Pasadena',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_json(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data.get('@graph', []) if isinstance(data, dict) else []
        for node in nodes:
            if isinstance(node, dict) and node.get('@type') == 'Event':
                return node
    return None


def parse_time_line(value):
    if not re.search(r'[ap]\.?m\.?\b', value, re.IGNORECASE):
        return []
    matches = list(TIME_RE.finditer(value))
    if not matches:
        return []

    meridiems = [match.group(3) for match in matches]
    times = []
    for index, match in enumerate(matches):
        meridiem = meridiems[index]
        if not meridiem:
            meridiem = next((item for item in meridiems[index + 1:] if item), None)
        if not meridiem:
            meridiem = next((item for item in reversed(meridiems[:index]) if item), None)
        if not meridiem:
            continue
        value = f'{match.group(1)}:{match.group(2) or "00"} {meridiem.replace(".", "").upper()}'
        try:
            parsed = datetime.strptime(value, '%I:%M %p').strftime('%H:%M')
        except ValueError:
            continue
        if parsed not in times:
            times.append(parsed)
    return times


def occurrences(description, start_date, end_date):
    lines = [line for line in clean_text(description).splitlines() if line]
    found = []
    for index, line in enumerate(lines):
        match = DATE_LINE_RE.fullmatch(line)
        if not match:
            continue
        month, first_day, second_day, year = match.groups()
        days = [first_day] + ([second_day] if second_day else [])
        dates = []
        for day in days:
            try:
                dates.append(datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat())
            except ValueError:
                pass
        times = parse_time_line(lines[index + 1]) if index + 1 < len(lines) else []
        for event_date in dates:
            found.append((event_date, times or [None]))

    if found:
        return found

    try:
        start = datetime.fromisoformat(start_date.replace('Z', '+00:00')).date().isoformat()
        end = datetime.fromisoformat((end_date or start_date).replace('Z', '+00:00')).date().isoformat()
    except (ValueError, AttributeError):
        return []
    return [(start, [None])] if start == end else []


def parse_event(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    event = event_json(soup)
    if not event:
        return []

    title = clean_text(event.get('name'))
    location = event.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    city = city or VENUE_CITY_DEFAULTS.get(venue, '')
    description_node = soup.select_one('.tribe-events-single-event-description')
    description = clean_text(description_node.get_text('\n', strip=True)) if description_node else ''
    if not description:
        description = clean_text(event.get('description'))

    if not all((title, venue, city, response.url)):
        return []

    records = []
    for event_date, times in occurrences(description, event.get('startDate'), event.get('endDate')):
        for event_time in times:
            records.append({
                'title': title,
                'date': event_date,
                'url': response.url,
                'time_from': event_time,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response)


def scrape_concerts():
    response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.content, 'xml')
    urls = [
        node.get_text(strip=True)
        for node in sitemap.find_all('loc')
        if '/concert/' in node.get_text(strip=True)
    ]

    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_event, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Event request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
            except Exception as error:
                log_message(
                    'Event parsing failed',
                    event='crawler_event_parse_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PasadenaSymphonyPopsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pasadenasymphony_pops_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PasadenaSymphonyPopsOrgCrawler().run()


if __name__ == '__main__':
    main()
