import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.blackpearlco.org/'
PERFORMANCES_URL = urljoin(SOURCE_URL, 'performances')
SOURCE = 'Black Pearl Chamber Orchestra'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ').replace('\u200d', '')).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def labelled_value(soup, label):
    node = soup.find(
        ['strong', 'b'],
        string=lambda value: value and clean_text(value).rstrip(':').lower() == label.lower(),
    )
    if node:
        container = node.parent
    else:
        container = soup.find(
            ['p', 'div'],
            string=lambda value: value
            and re.match(rf'^\s*{re.escape(label)}\s*:', clean_text(value), re.I),
        )
    if not container:
        return ''
    text = clean_text(container)
    return re.sub(rf'^{re.escape(label)}\s*:\s*', '', text, flags=re.I).strip()


def venue_and_city(value):
    value = clean_text(value)
    if not value:
        return '', ''

    # The venue field sometimes appends a postal address after a colon.
    venue = re.split(r':\s*(?=\d)', value, maxsplit=1)[0].strip(' :')
    lowered = venue.lower()
    if 'barnes foundation' in lowered:
        return 'The Barnes Foundation', 'Philadelphia'
    if 'salem baptist church' in lowered:
        return 'Salem Baptist Church of Abington', 'Abington'
    return venue, ''


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event_date = parse_date(labelled_value(soup, 'Date'))
    venue, city = venue_and_city(labelled_value(soup, 'Venue'))

    heading = soup.find('h1')
    title = clean_text(heading)
    event_name = labelled_value(soup, 'Event')
    if event_name:
        title = event_name

    details = labelled_value(soup, 'Details')
    if details and title.lower().startswith('salem baptist church'):
        title = details

    description_parts = []
    for label in ('Details',):
        value = details
        if value and value not in description_parts:
            description_parts.append(value)

    if not all((title, event_date, venue, city)):
        log_message(
            'Skipping performance with incomplete required fields',
            event='crawler_record_skipped',
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
        'time_from': parse_time(labelled_value(soup, 'Time')),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(PERFORMANCES_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    urls = []
    for link in soup.select('a[href*="/performances/"]'):
        url = urljoin(PERFORMANCES_URL, link.get('href'))
        if url not in urls:
            urls.append(url)

    records = []
    for url in urls:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            record = parse_detail(detail_response.text, url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Could not retrieve performance detail',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No complete linked performances found',
            event='crawler_empty_listing',
            level='warning',
            url=PERFORMANCES_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BlackPearlCoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='blackpearlco_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BlackPearlCoOrgCrawler().run()


if __name__ == '__main__':
    main()
