import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lubbocksymphony.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Lubbock Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2,
    'mar': 3, 'march': 3, 'apr': 4, 'april': 4, 'may': 5,
    'jun': 6, 'june': 6, 'jul': 7, 'july': 7, 'aug': 8,
    'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}

OCCURRENCE_RE = re.compile(
    r'\b(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),?\s+'
    r'(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|'
    r'JUL(?:Y)?|AUG(?:UST)?|SEPT?(?:EMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|'
    r'DEC(?:EMBER)?)\.?\s+(\d{1,2})(?:,\s+(20\d{2}))?\s+'
    r'(?:AT|\|)\s+(\d{1,2}):(\d{2})\s*([AP]M)\s+(?:IN|\|)\s+([^\n]+)',
    re.IGNORECASE,
)


def clean_text(value):
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value or '')
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    heading = main.select_one('h1, h2') if main else None
    title = clean_text(heading)
    description = clean_text(main)
    match = OCCURRENCE_RE.search(description)
    if not title or not match:
        return None

    month = MONTHS.get(match.group(1).lower().rstrip('.'))
    year = match.group(3)
    if not year:
        season = re.search(r'\b(\d{2})-(\d{2})\s+Season\b', description, re.IGNORECASE)
        if not season:
            return None
        year = f'20{season.group(1) if month >= 7 else season.group(2)}'
    try:
        event_date = date(int(year), month, int(match.group(2))).isoformat()
    except (TypeError, ValueError):
        return None

    hour = int(match.group(4)) % 12
    if match.group(6).upper() == 'PM':
        hour += 12
    venue = clean_text(match.group(7)).strip(' |')
    if not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{hour:02d}:{match.group(5)}',
        'venue': venue,
        'city': 'Lubbock',
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def event_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.select('main a[href]'):
        if 'tickets & more info' not in clean_text(link).lower():
            continue
        url = urljoin(CONCERTS_URL, link.get('href'))
        if url.startswith(SOURCE_URL) and url not in urls:
            urls.append(url)
    return urls


class LubbockSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lubbocksymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        records = []
        for url in event_urls(response.text):
            try:
                detail = requests.get(url, headers=HEADERS, timeout=45)
                detail.raise_for_status()
                record = parse_event(detail.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Lubbock Symphony concert detail',
                    event='crawler_item_failed',
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
                    'Skipped incomplete Lubbock Symphony concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, time, or venue is missing',
                )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'], item['title']),
        )


def main():
    LubbockSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
