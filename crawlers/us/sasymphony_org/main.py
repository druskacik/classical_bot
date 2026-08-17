import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sasymphony.org/'
SOURCE = 'San Antonio Symphony'
CITY = 'San Antonio'
VENUE = 'Tobin Center for the Performing Arts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_FORMATS = (
    '%I:%M%p; %a, %b %d, %Y',
    '%I:%M%p, %A, %B %d, %Y',
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_start(value):
    value = clean_text(value)
    for date_format in DATE_FORMATS:
        try:
            start = datetime.strptime(value, date_format)
            return start.date().isoformat(), start.strftime('%H:%M')
        except ValueError:
            continue
    return None, None


def event_url(card):
    link = card.select_one('.vem-more-details a[href]')
    if not link:
        link = card.select_one('a[href*="event/"]')
    return urljoin(SOURCE_URL, link.get('href')) if link else ''


def card_description(card):
    field_set = card.select_one('.vem-single-event-field-set')
    if not field_set:
        return None
    lines = [clean_text(node.get_text(' ', strip=True)) for node in field_set.select('.one-field')]
    lines = [line for line in lines if line]
    return '\n'.join(lines) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SOURCE_URL, timeout=45)
    response.raise_for_status()
    response.encoding = 'utf-8-sig'
    soup = BeautifulSoup(response.text, 'html.parser')

    # The archived site redirects its calendar and detail URLs to the homepage.
    # This first-party listing is therefore the complete concert feed it still
    # publishes.  Limit it to the site's explicit Classics category.
    records = []
    descriptions = {}
    for card in soup.select('.vem-single-event.vem-cat-classics'):
        title_node = card.select_one('.vem-single-event-title')
        title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
        url = event_url(card)
        description = card_description(card)
        if description and url:
            descriptions[url] = description

        for occurrence in card.select('.vem-one-occurrence'):
            start_node = occurrence.select_one('.vem-single-event-date-start')
            event_date, time_from = parse_start(
                start_node.get_text(' ', strip=True) if start_node else ''
            )
            if not title or not url or not event_date:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': VENUE,
                'city': CITY,
                'country_code': 'US',
                'description': description,
            })

    for record in records:
        record['description'] = descriptions.get(record['url']) or record['description']

    if not records:
        log_message(
            'No San Antonio Symphony concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class SaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
