import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nationalphilharmonic.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/?mode=list')
SOURCE = 'National Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'The Music Center at Strathmore': 'North Bethesda',
    'Music Center at Strathmore': 'North Bethesda',
    'Strathmore': 'North Bethesda',
    'Capital One Hall': 'Tysons',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*[·|]\s*'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None
    raw_time = match.group(2).replace(' ', '').upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return event_date, datetime.strptime(raw_time, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return event_date, None


def extract_description(soup):
    body = soup.select_one('.primary .body')
    if not body:
        return None
    body = BeautifulSoup(str(body), 'html.parser')
    for node in body.select('a.btn, a.ghostbtn, figure img'):
        node.decompose()
    for heading in body.select('h1, h2, h3, h4'):
        if clean_text(heading.get_text(' ', strip=True)).lower() in {
            'tickets',
            'tickets & subscriptions',
        }:
            heading.decompose()
    text = clean_text(body.get_text('\n', strip=True))
    text = re.sub(
        r'\n?Programs, artists, dates, prices, and availability subject to change\.?$',
        '',
        text,
        flags=re.IGNORECASE,
    ).strip()
    return text or None


def parse_detail(session, url, listing_title, listing_date_text):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    title_node = soup.select_one('h1')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else listing_title)
    date_node = soup.select_one('.date')
    date_text = clean_text(date_node.get_text('\n', strip=True) if date_node else listing_date_text)
    event_date, time_from = parse_date_time(date_text)

    venue = ''
    if date_node:
        lines = [clean_text(line) for line in date_node.get_text('\n').splitlines()]
        lines = [line for line in lines if line]
        if len(lines) > 1:
            venue = lines[-1]
    city = VENUE_CITIES.get(venue, '')

    if not title or not event_date or not venue or not city:
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
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': extract_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    seen_urls = set()
    for listing in soup.select('.listing .info'):
        link = listing.find('a', href=True)
        date_node = listing.select_one('.date')
        if not link or not date_node:
            continue
        url = urljoin(SOURCE_URL, link['href'])
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            record = parse_detail(
                session,
                url,
                clean_text(link.get_text(' ', strip=True)),
                clean_text(date_node.get_text(' ', strip=True)),
            )
        except requests.RequestException as error:
            log_message(
                'Could not fetch event detail',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class NationalPhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nationalphilharmonic_org',
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
        return scrape_concerts()


def main():
    NationalPhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
