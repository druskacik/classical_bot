import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cgsuny.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/year')
SOURCE = 'The Classical Guitar Society of Upstate New York'
FIRST_YEAR = 2018
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def card_body(container, heading):
    for card in container.select('.card'):
        header = card.select_one('.card-header')
        if header and clean_text(header).casefold() == heading.casefold():
            return card.select_one('.card-body')
    return None


def parse_date(value):
    normalized = re.sub(r'(\d)(?:st|nd|rd|th)', r'\1', value, flags=re.I)
    match = re.search(r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})', normalized)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])M\b', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'P' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_location(where):
    lines = [line for line in clean_text(where).splitlines() if line != 'Get Directions']
    if len(lines) < 2:
        return None
    venue = lines[0]
    city = None
    for line in reversed(lines[1:]):
        match = re.match(r'^(.+?),\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$', line)
        if match:
            city = match.group(1).strip()
            break
    if not venue or not city:
        return None
    return venue, city


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main') or soup
    content = main.select_one('.col-lg-9 > .card > .card-body')
    title_node = content.select_one('h1') if content else None
    when = card_body(content, 'When') if content else None
    where = card_body(content, 'Where') if content else None
    title = clean_text(title_node)
    event_date = parse_date(clean_text(when))
    location = parse_location(where) if where else None
    if not title or not event_date or not location:
        return None

    description_container = BeautifulSoup(str(content), 'html.parser')
    for node in description_container.select('h1, .float-sm-start'):
        node.decompose()
    description = clean_text(description_container)

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(when)),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    event_urls = set()
    current_year = datetime.now(timezone.utc).year

    for year in range(FIRST_YEAR, current_year + 3):
        year_url = f'{CALENDAR_URL}/{year}'
        try:
            response = session.get(year_url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch CGSUNY calendar year',
                event='crawler_page_failed',
                level='warning',
                url=year_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.select('a[href*="/events/"]'):
            if link.get('href'):
                event_urls.add(urljoin(SOURCE_URL, link['href']))

    records = []
    for url in sorted(event_urls):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            record = parse_event(response.text, response.url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch CGSUNY event',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
        else:
            log_message(
                'Skipped CGSUNY event with incomplete details',
                event='crawler_record_skipped',
                level='warning',
                url=url,
            )

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue'], item['url']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))
    if not result:
        log_message(
            'No valid CGSUNY events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return result


class CgsunyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cgsuny_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CgsunyOrgCrawler().run()


if __name__ == '__main__':
    main()
