import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bitterrootbaroque.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'event-list')
ARCHIVE_URL = urljoin(SOURCE_URL, 'past-concerts')
SOURCE = 'Bitterroot Baroque'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(20\d{2})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).upper() == 'PM' and hour != 12:
        hour += 12
    if match.group(3).upper() == 'AM' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def page_lines(element):
    lines = []
    for node in element.select('h1, h2, h3, h4, h5, h6, p'):
        text = re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip(' \u200b')
        text = re.sub(r'(?<=\d)\s+(?=\d)', '', text)
        for weekday in (
            'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'
        ):
            text = re.sub(r'\s*'.join(weekday), weekday, text, flags=re.IGNORECASE)
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return lines


def location_from_lines(lines, date_index):
    previous_date = max(
        (index for index in range(date_index) if DATE_RE.search(lines[index])), default=-1
    )
    next_date = next(
        (index for index in range(date_index + 1, len(lines)) if DATE_RE.search(lines[index])),
        len(lines),
    )
    # Most cards put location after the date. A few newer archive blocks put
    # it immediately before the date, so use that only as a fallback.
    after = lines[date_index + 1:next_date]
    before = lines[previous_date + 1:date_index]

    def find_city(segment):
        for line in segment:
            lowered = line.lower()
            if 'hamilton' in lowered:
                return 'Hamilton'
            if 'philipsburg' in lowered:
                return 'Philipsburg'
            if 'missoula' in lowered:
                return 'Missoula'
        return None

    def find_venue(segment):
        for line in segment:
            lowered = line.lower().rstrip(',')
            if any(word in lowered for word in ('church', 'opera house', 'theatre', 'theater')):
                if not re.search(r'\d{3,}|,\s*(?:mt|montana)\b', lowered):
                    return line.rstrip(',')
        return None

    city = find_city(after) or find_city(list(reversed(before)))
    venue = find_venue(after) or find_venue(list(reversed(before)))
    return venue, city


def archive_title(lines):
    intro = []
    for line in lines:
        if DATE_RE.search(line) or parse_time(line):
            break
        if 'church' in line.lower() or 'opera house' in line.lower():
            continue
        if re.search(r'\d{3,}|,\s*(?:MT|Montana)\b', line):
            continue
        if re.fullmatch(r'postponed\s*:', line, re.IGNORECASE):
            continue
        if line.lower() == 'bitterroot baroque presents':
            continue
        intro.append(line)
    return ' – '.join(intro[:2]).strip()


def records_from_archive_block(element, title=None):
    lines = page_lines(element)
    title = title or archive_title(lines)
    description = '\n'.join(lines) or None
    records = []
    for index, line in enumerate(lines):
        event_date = parse_date(line)
        if not event_date:
            continue
        venue, city = location_from_lines(lines, index)
        if not title or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': ARCHIVE_URL,
            'time_from': parse_time(line),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_archive(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []

    christmas = soup.find(id='comp-m5jnxkiq3')
    if christmas:
        records.extend(records_from_archive_block(christmas))

    french_details = soup.find(id='comp-m3iyf1k82')
    if french_details:
        heading = soup.find('h1', string=re.compile(r'Music from French Opera', re.I))
        title = clean_text(heading) if heading else 'Music from French Opera'
        records.extend(records_from_archive_block(french_details, title))

    bach = soup.find(id='comp-lzlqjyuo3')
    if bach:
        records.extend(records_from_archive_block(bach, 'Music of Johann Sebastian Bach'))

    for block in soup.find_all(id=re.compile(r'^comp-krc29brt__item')):
        records.extend(records_from_archive_block(block))
    return records


def json_events(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                yield item


def record_from_json_event(event, fallback_url):
    location = event.get('location') or {}
    address = location.get('address') or {}
    if isinstance(address, str):
        address = {'streetAddress': address}
    title = clean_text(event.get('name'))
    start = str(event.get('startDate') or '')
    try:
        parsed_start = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except ValueError:
        return None
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    url = urljoin(SOURCE_URL, event.get('url') or fallback_url)
    if not title or not venue or not city or not url:
        return None
    return {
        'title': title,
        'date': parsed_start.date().isoformat(),
        'url': url,
        'time_from': parsed_start.strftime('%H:%M') if 'T' in start else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BitterrootbaroqueOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bitterrootbaroque_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events_response = session.get(EVENTS_URL, timeout=45)
            events_response.raise_for_status()
            archive_response = session.get(ARCHIVE_URL, timeout=45)
            archive_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Bitterroot Baroque concerts',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_archive(archive_response.text)
        listing_soup = BeautifulSoup(events_response.text, 'html.parser')
        detail_urls = {
            urljoin(EVENTS_URL, link['href'])
            for link in listing_soup.select('a[href*="event-details"]')
        }
        for event in json_events(listing_soup):
            record = record_from_json_event(event, EVENTS_URL)
            if record:
                records.append(record)
        for url in sorted(detail_urls):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Bitterroot Baroque event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for event in json_events(BeautifulSoup(response.text, 'html.parser')):
                record = record_from_json_event(event, url)
                if record:
                    records.append(record)

        unique = {
            (record['title'], record['date'], record['time_from'], record['venue']): record
            for record in records
        }
        return sorted(
            unique.values(),
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    BitterrootbaroqueOrgCrawler().run()


if __name__ == '__main__':
    main()
