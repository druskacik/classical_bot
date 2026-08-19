import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.riversidesymphony.org/'
SOURCE = 'Riverside Symphony'
HEADERS = {
    'Accept': 'application/json, text/html;q=0.9',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}
DATE_RE = re.compile(
    r'^(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s*'
    r'(?P<month>[A-Z]+)\s+(?P<day>\d{1,2}),\s+AT\s+(?P<time>.+)$',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'(?P<start>20\d{2})\s*[-–]\s*(?P<end>20\d{2})\s+SEASON', re.IGNORECASE)


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def discover_season_urls(session):
    response = session.get(SOURCE_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = []
    for link in soup.select('a[href]'):
        label = clean_text(link.get_text(' ', strip=True))
        href = link.get('href', '')
        if 'season' not in label.casefold() and 'season' not in href.casefold():
            continue
        url = urljoin(SOURCE_URL, href).split('#', 1)[0]
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc and url not in urls:
            urls.append(url)
    return urls


def parse_time(value):
    normalized = clean_text(value).upper().replace('.', '')
    if normalized == 'NOON':
        return '12:00'
    if normalized == 'MIDNIGHT':
        return '00:00'
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_season_page(payload, page_url):
    content = payload.get('mainContent')
    if not content:
        return []
    soup = BeautifulSoup(content, 'html.parser')
    elements = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'p'])

    season = None
    for element in elements:
        match = SEASON_RE.search(clean_text(element.get_text(' ', strip=True)))
        if match:
            season = (int(match.group('start')), int(match.group('end')))
            break
    if not season:
        return []

    records = []
    for index, element in enumerate(elements):
        date_match = DATE_RE.match(clean_text(element.get_text(' ', strip=True)))
        if element.name != 'h2' or not date_match:
            continue

        following = []
        for candidate in elements[index + 1:]:
            if candidate.name == 'h2' and DATE_RE.match(clean_text(candidate.get_text(' ', strip=True))):
                break
            following.append(candidate)

        venue = next(
            (clean_text(item.get_text(' ', strip=True)) for item in following if item.name == 'h3'),
            '',
        )
        title_item = next((item for item in following if item.name == 'h5'), None)
        title = clean_text(title_item.get_text(' ', strip=True)) if title_item else ''
        if not venue or not title:
            continue

        month = datetime.strptime(date_match.group('month')[:3], '%b').month
        year = season[0] if month >= 7 else season[1]
        try:
            date = datetime(year, month, int(date_match.group('day'))).date().isoformat()
        except ValueError:
            continue

        description_parts = []
        after_title = False
        for item in following:
            if item is title_item:
                after_title = True
                continue
            if not after_title or item.find_parent(['h1', 'h2', 'h3', 'h4', 'h5', 'p']):
                continue
            text = clean_text(item.get_text(' ', strip=True))
            if text and text not in description_parts:
                description_parts.append(text)

        records.append({
            'title': title,
            'date': date,
            'url': page_url,
            'time_from': parse_time(date_match.group('time')),
            'venue': venue,
            'city': 'New York',
            'country_code': 'US',
            'description': '\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    season_urls = discover_season_urls(session)
    records = []
    for url in season_urls:
        try:
            response = session.get(url, params={'format': 'json'}, headers=HEADERS, timeout=60)
            response.raise_for_status()
            records.extend(parse_season_page(response.json(), url))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Unable to parse Riverside Symphony season page',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No Riverside Symphony concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class RiversideSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='riversidesymphony_org',
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
    RiversideSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
