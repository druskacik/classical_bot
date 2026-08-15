import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chambermusic.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-events/')
SOURCE = 'Friends of Chamber Music Kansas City'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_datetime(value):
    value = clean_text(value)
    for pattern in ('%I:%M%p, %A, %B %d, %Y', '%I%p, %A, %B %d, %Y'):
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            pass
    return None, None


def parse_city(value):
    value = clean_text(value)
    match = re.match(
        r'(.+?),\s*(?:[A-Z]{2}|[A-Za-z][A-Za-z ]+)\s+\d{5}(?:-\d{4})?$',
        value,
    )
    return clean_text(match.group(1)) if match else ''


def description_from_page(soup):
    parts = []
    for selector in (
        '.vem-single-event-field-set.field-set-one',
        '.vem-single-event-field-set.field-set-two',
        '.vem-single-event-details',
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        text = node.get_text('\n', strip=True)
        text = re.sub(r'[ \t]+', ' ', text.replace('\xa0', ' '))
        text = re.sub(r' *\n *', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event_page(soup, url):
    title = clean_text(soup.select_one('h1'))
    description = description_from_page(soup)
    records = []

    for occurrence in soup.select('.vem-one-occurrence'):
        event_date, time_from = parse_datetime(
            occurrence.select_one('.vem-single-event-date-start')
        )
        venue = clean_text(occurrence.select_one('.vem-single-occurrence-venue'))
        city = parse_city(occurrence.select_one('.venue-city'))
        if not all((title, event_date, venue, city)):
            log_message(
                'Skipping event occurrence with incomplete required fields',
                event='crawler_occurrence_skipped',
                level='warning',
                url=url,
            )
            continue
        records.append({
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
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    listing = BeautifulSoup(response.text, 'html.parser')

    urls = []
    for card in listing.select('.vem-single-event'):
        link = card.select_one('.vem-more-details a[href], a[href*="/event/"]')
        if link:
            url = urljoin(LISTING_URL, link.get('href'))
            if url not in urls:
                urls.append(url)

    records = []
    for url in urls:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            records.extend(parse_event_page(BeautifulSoup(detail_response.text, 'html.parser'), url))
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusic_org',
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
    ChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
