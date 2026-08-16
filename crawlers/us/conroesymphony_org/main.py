import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://conroesymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events/')
SOURCE = 'Conroe Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'\b([A-Za-z]+\s+\d{1,2},\s+\d{4})\b')
TIME_RE = re.compile(r'Concert\s+Begins\s+at\s+(\d{1,2}(?::\d{2})?\s*[ap]m)', re.I)
CITY_RE = re.compile(r'\b([^\n,]+),\s*TX\s+\d{5}(?:-\d{4})?\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    normalized = re.sub(r'\s+', ' ', match.group(1).upper()).strip()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def listing_items(soup):
    items = []
    seen = set()
    for link in soup.select('a[href]'):
        if clean_text(link.get_text(' ', strip=True)).lower() != 'learn more':
            continue
        url = urljoin(EVENTS_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc.lower() != urlparse(SOURCE_URL).netloc or url in seen:
            continue

        card = link.find_parent(class_=re.compile(r'\bet_pb_column\b'))
        card_text = clean_text(card.get_text('\n', strip=True)) if card else ''
        event_date = parse_date(card_text)
        if not event_date:
            continue
        seen.add(url)
        items.append((url, event_date))
    return items


def detail_record(soup, url, event_date):
    body_text = clean_text(soup.get_text('\n', strip=True))
    title_node = soup.select_one('h1')
    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''

    location_match = re.search(
        r'LOCATION\s*\n+([^\n]+)\s*\n+[^\n]*\n+([^\n]+,\s*TX\s+\d{5}(?:-\d{4})?)',
        body_text,
        re.I,
    )
    venue = clean_text(location_match.group(1)) if location_match else ''
    city_match = CITY_RE.search(location_match.group(2)) if location_match else None
    city = clean_text(city_match.group(1)) if city_match else ''

    description = None
    location_end = location_match.end() if location_match else None
    tickets_match = re.search(
        r'\nTICKETS\n', body_text[location_end:] if location_end else '', re.I
    )
    if location_end is not None and tickets_match:
        content = body_text[location_end:location_end + tickets_match.start()]
        content = re.sub(r'^\s*Map\s*', '', content, flags=re.I)
        description = clean_text(content) or None

    if not all((title, event_date, venue, city)):
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(event_date),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(body_text),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ConroeSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='conroesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
        session = requests.Session()
        session.headers.update(HEADERS)

        response = session.get(EVENTS_URL, timeout=45)
        response.raise_for_status()
        items = listing_items(BeautifulSoup(response.text, 'html.parser'))

        records = []
        for url, event_date in items:
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                record = detail_record(
                    BeautifulSoup(detail_response.text, 'html.parser'), url, event_date
                )
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Unable to fetch concert detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        if not records:
            log_message(
                'No concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=EVENTS_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ConroeSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
