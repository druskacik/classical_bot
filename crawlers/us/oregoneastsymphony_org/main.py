import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oregoneastsymphony.org/'
SOURCE = 'Oregon East Symphony'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
DATE_LINE_RE = re.compile(
    r'(?P<time>\d{1,2}:\d{2}\s*[ap]m)\s*,?\s*'
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*'
    r'(?P<date>[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*\d{4})\s*'
    r'\|\s*(?P<location>.+)$',
    re.IGNORECASE,
)
SEASON_PATH_RE = re.compile(r'^/\d{4}-\d{4}-season/?$', re.IGNORECASE)


def clean_text(value):
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def parse_date(value):
    normalized = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', value, flags=re.IGNORECASE)
    normalized = re.sub(r'\s+', ' ', normalized.replace(',', ' ')).strip()
    try:
        return datetime.strptime(normalized, '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    try:
        return datetime.strptime(value.replace(' ', '').upper(), '%I:%M%p').strftime('%H:%M')
    except ValueError:
        return None


def parse_location(value):
    value = clean_text(value)
    match = re.match(r'(?P<venue>.+?)\s*\((?P<address>[^()]*)\)\s*$', value)
    if not match:
        return None, None
    venue = clean_text(match.group('venue'))
    address = clean_text(match.group('address'))
    city_match = re.search(r',\s*([^,]+),\s*OR(?:\s+\d{5}(?:-\d{4})?)?\s*$', address, re.IGNORECASE)
    city = clean_text(city_match.group(1)) if city_match else None
    return venue or None, city


def parse_season_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    paragraphs = [clean_text(node.get_text(' ', strip=True)) for node in soup.find_all('p')]
    paragraphs = [text for text in paragraphs if text]
    records = []

    index = 0
    while index < len(paragraphs) - 1:
        title = paragraphs[index]
        if not DATE_LINE_RE.search(paragraphs[index + 1]):
            index += 1
            continue

        date_index = index + 1
        occurrences = []
        while date_index < len(paragraphs):
            match = DATE_LINE_RE.search(paragraphs[date_index])
            if not match:
                break
            event_date = parse_date(match.group('date'))
            venue, city = parse_location(match.group('location'))
            if event_date and venue and city:
                occurrences.append((event_date, parse_time(match.group('time')), venue, city))
            date_index += 1

        description = None
        if date_index < len(paragraphs) and not DATE_LINE_RE.search(paragraphs[date_index]):
            description = paragraphs[date_index]

        for event_date, time_from, venue, city in occurrences:
            records.append({
                'title': title,
                'date': event_date,
                'url': page_url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
        index = max(date_index + 1, index + 1)

    return records


def discover_season_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.find_all('a', href=True):
        url = urljoin(SOURCE_URL, link['href'])
        parsed = urlparse(url)
        if parsed.netloc.lower() == 'www.oregoneastsymphony.org' and SEASON_PATH_RE.match(parsed.path):
            urls.append(url.rstrip('/'))
    return list(dict.fromkeys(urls))


class OregonEastSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oregoneastsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            homepage = session.get(SOURCE_URL, timeout=45)
            homepage.raise_for_status()
            season_urls = discover_season_urls(homepage.text)
            records = []
            for url in season_urls:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_season_page(response.text, url))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Oregon East Symphony concerts',
                event='crawler_fetch_failed',
                level='error',
                url=getattr(getattr(error, 'request', None), 'url', SOURCE_URL),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not season_urls:
            log_message(
                'No Oregon East Symphony season pages were discovered',
                event='crawler_no_season_pages',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return records


def main():
    return OregonEastSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
