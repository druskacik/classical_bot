import json
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.boulderoperacompany.com/'
SOURCE = 'Boulder Opera Company'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>20\d{2}))?'
    r'(?:\s*(?:at|\|)\s*(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M))?',
    re.IGNORECASE,
)


def clean_text(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def parse_time(value):
    if not value:
        return None
    value = clean_text(value).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_date_match(match):
    year = int(match.group('year') or date.today().year)
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def place_parts(value):
    value = re.sub(r'^where\s*:\s*', '', clean_text(value), flags=re.I)
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return '', ''
    venue = parts[0]
    city = parts[-2] if len(parts) >= 3 and re.fullmatch(r'[A-Z]{2}(?:\s+\d{5})?', parts[-1]) else parts[1]
    if re.fullmatch(r'\d+.*', venue) or not venue or not city:
        return '', ''
    return venue, city


def iter_json_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from iter_json_objects(item)
    elif isinstance(value, dict):
        yield value
        for item in value.get('@graph', []):
            yield from iter_json_objects(item)


def records_from_json_ld(soup, url):
    records = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in iter_json_objects(payload):
            if item.get('@type') not in {'Event', 'MusicEvent'} or not item.get('startDate'):
                continue
            location = item.get('location') or {}
            address = location.get('address') or {}
            if isinstance(address, str):
                city = ''
            else:
                city = clean_text(address.get('addressLocality'))
            venue = clean_text(location.get('name'))
            try:
                start = datetime.fromisoformat(item['startDate'].replace('Z', '+00:00'))
            except (ValueError, TypeError):
                continue
            title = clean_text(item.get('name'))
            if not all((title, venue, city)):
                continue
            records.append(make_record(
                title, start.date().isoformat(), url, start.strftime('%H:%M'),
                venue, city, clean_text(item.get('description')) or None,
            ))
    return records


def records_from_page(soup, url):
    main = soup.select_one('main') or soup
    nodes = []
    for node in main.select('h1, h2, h3, h4, p'):
        text = clean_text(node.get_text(' ', strip=True))
        if text:
            nodes.append((node.name, text))

    title_node = next((text for tag, text in nodes if tag == 'h1'), '')
    title = re.sub(r'\s*[—|-]\s*Boulder Opera Company$', '', title_node).strip()
    if not title:
        return []

    section_start = next(
        (index for index, (_, text) in enumerate(nodes) if text.lower() in {'when', 'performances'}),
        None,
    )
    if section_start is None:
        return []

    description_parts = []
    for tag, text in nodes[1:section_start]:
        if tag == 'p' and text.lower() != title.lower() and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    pending = []
    records = []
    section_nodes = nodes[section_start + 1:]
    index = 0
    while index < len(section_nodes):
        tag, text = section_nodes[index]
        if tag.startswith('h') and text.lower() not in {'where'}:
            break
        match = DATE_RE.search(text)
        if match:
            event_date = parse_date_match(match)
            if event_date:
                pending.append((event_date, parse_time(match.group('time'))))
        if re.match(r'^where\s*:', text, re.I):
            venue, city = place_parts(text)
            if venue and city:
                records.extend(
                    make_record(title, event_date, url, event_time, venue, city, description)
                    for event_date, event_time in pending
                )
            pending = []
        elif text.lower() == 'where':
            place_lines = []
            lookahead = index + 1
            while lookahead < len(section_nodes) and not section_nodes[lookahead][0].startswith('h'):
                place_lines.append(section_nodes[lookahead][1])
                lookahead += 1
            venue, city = place_parts(', '.join(place_lines[:2]))
            if venue and city:
                records.extend(
                    make_record(title, event_date, url, event_time, venue, city, description)
                    for event_date, event_time in pending
                )
            pending = []
            index = lookahead - 1
        index += 1
    return records


def make_record(title, event_date, url, time_from, venue, city, description):
    return {
        'title': title,
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


def event_urls(soup):
    urls = set()
    for folder in soup.select('.folder-parent'):
        label = folder.select_one(':scope > a')
        if not label or clean_text(label.get_text()).lower() not in {"what's on", 'what’s on'}:
            continue
        for link in folder.select('.folder-child a[href]'):
            url = urljoin(SOURCE_URL, link['href'])
            if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
                urls.add(url.split('#')[0])
    return sorted(urls)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    urls = event_urls(BeautifulSoup(response.text, 'html.parser'))

    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            page_records = records_from_json_ld(soup, url) or records_from_page(soup, url)
            records.extend(page_records)
            if not page_records:
                log_message('Event page could not be parsed', event='crawler_event_skipped', level='warning', url=url)
        except requests.RequestException as error:
            log_message(
                'Event page request failed', event='crawler_event_request_failed', level='warning',
                url=url, error_type=type(error).__name__, error_message=str(error),
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BoulderOperaCompanyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='boulderoperacompany_com',
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
    BoulderOperaCompanyComCrawler().run()


if __name__ == '__main__':
    main()
