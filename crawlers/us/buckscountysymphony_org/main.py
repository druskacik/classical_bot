import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.buckscountysymphony.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts/')
PAST_URL = urljoin(SOURCE_URL, 'past/')
SOURCE = 'Bucks County Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_FORMATS = ('%A, %B %d, %Y', '%B %d, %Y')
CITY_RE = re.compile(r'^(.+?),\s*PA(?:\s+\d{5}(?:-\d{4})?)?$', re.I)
STREET_RE = re.compile(r'^\d+\s')
KNOWN_VENUE_CITIES = {
    'Delaware Valley University Life Sciences Building': 'Doylestown',
    'Delaware Valley University – Life Sciences Building': 'Doylestown',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = clean_text(value)
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    return ''


def parse_time(value):
    match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*[AP]M)\b', clean_text(value), re.I)
    if not match:
        return None
    for date_format in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(match.group(1).upper(), date_format).strftime('%H:%M')
        except ValueError:
            pass
    return None


def archive_urls(soup):
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(PAST_URL, link.get('href'))
        if re.fullmatch(rf'{re.escape(SOURCE_URL)}(?:seasons|soiree)/\d{{4}}-\d{{4}}/', url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def detail_urls(soup, page_url):
    urls = []
    for card in soup.select('.grid-blog'):
        link = card.find('a', href=True)
        if not link:
            continue
        url = urljoin(page_url, link.get('href'))
        if url.startswith(urljoin(SOURCE_URL, 'concerts/')):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_location(value):
    lines = [clean_text(line) for line in clean_text(value).splitlines()]
    lines = [line for line in lines if line]
    city = ''
    city_index = None
    for index in range(len(lines) - 1, -1, -1):
        match = CITY_RE.match(lines[index])
        if match:
            city = clean_text(match.group(1))
            city_index = index
            break
    if city_index is None:
        venue = ' – '.join(lines)
        return venue, KNOWN_VENUE_CITIES.get(venue, '')

    venue_lines = lines[:city_index]
    while venue_lines and STREET_RE.match(venue_lines[-1]):
        venue_lines.pop()
    venue = ' – '.join(venue_lines)
    return venue, city


def parse_detail(soup, url):
    article = soup.select_one('article.bcso_concerts')
    if not article:
        return None

    title_node = article.find('h3')
    metadata = article.find_all('h5', recursive=False)
    if not title_node or len(metadata) < 2:
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    date_time_lines = [
        clean_text(line) for line in metadata[0].get_text('\n', strip=True).splitlines()
        if clean_text(line)
    ]
    event_date = parse_date(date_time_lines[0]) if date_time_lines else ''
    time_from = parse_time('\n'.join(date_time_lines[1:]))
    venue, city = parse_location(metadata[1].get_text('\n', strip=True))
    if not title or not event_date or not venue or not city:
        return None

    separator = article.select_one('.seperator')
    description_parts = []
    if separator:
        for node in separator.find_all_next(['h3', 'h4', 'p']):
            if node.find_parent('article') is not article:
                break
            text = clean_text(node.get_text(' ', strip=True))
            if text and text not in description_parts:
                description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    past_response = session.get(PAST_URL, timeout=45)
    past_response.raise_for_status()
    listing_urls = [CONCERTS_URL, *archive_urls(BeautifulSoup(past_response.text, 'html.parser'))]

    event_urls = []
    for listing_url in listing_urls:
        response = session.get(listing_url, timeout=45)
        response.raise_for_status()
        event_urls.extend(detail_urls(BeautifulSoup(response.text, 'html.parser'), listing_url))
    event_urls = list(dict.fromkeys(event_urls))

    records = []
    for url in event_urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            record = parse_detail(BeautifulSoup(response.text, 'html.parser'), url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping concert with incomplete required fields',
                    event='crawler_record_skipped',
                    level='warning',
                    url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
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
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BucksCountySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='buckscountysymphony_org',
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
    BucksCountySymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
