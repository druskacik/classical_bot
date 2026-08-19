import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operade.org/'
SOURCE = 'OperaDelaware'
SEASON_URL = urljoin(SOURCE_URL, '202627-operadelaware-season')
CITY = 'Wilmington'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        1,
    )
}
DATE_PATTERN = re.compile(
    r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:,\s*(\d{4}))?'
    r'(?:\s*\|\s*(\d{1,2}(?::\d{2})?\s*[AP]M))?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_time(value):
    if not value:
        return None
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*([AP]M)', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'PM':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def season_years(soup):
    text = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    match = re.search(r'\b(20\d{2})/(\d{2})\b', text)
    if not match:
        raise ValueError('Could not determine season years')
    start = int(match.group(1))
    end = (start // 100) * 100 + int(match.group(2))
    return start, end


def production_urls(soup, season_start, season_end):
    suffix = f'{str(season_start)[-2:]}{str(season_end)[-2:]}'
    urls = set()
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc == urlparse(SOURCE_URL).netloc and parsed.path.endswith(suffix):
            urls.add(url.split('#', 1)[0])
    return sorted(urls)


def detail_records(soup, url, season_start, season_end):
    title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    title = re.sub(r'\s+[—|-]\s+OperaDelaware$', '', title).strip()
    if not title:
        return []

    venue = None
    date_heading = None
    for heading in soup.select('h1, h2, h3'):
        text = clean_text(heading.get_text(' ', strip=True))
        if date_heading is None and DATE_PATTERN.search(text):
            date_heading = text
        match = re.search(r'Performances? at\s+(.+?)(?:\s+Sung\b|\s+Running\b|$)', text, re.I)
        if match:
            venue = clean_text(match.group(1))
    if not date_heading or not venue:
        return []

    content = soup.select_one('.main-content')
    if content is None:
        return []
    description = clean_text(content.get_text('\n', strip=True))
    description = description or None

    records = []
    for match in DATE_PATTERN.finditer(date_heading):
        month = MONTHS[match.group(1).capitalize()]
        year = int(match.group(3)) if match.group(3) else (
            season_start if month >= 7 else season_end
        )
        try:
            event_date = date(year, month, int(match.group(2))).isoformat()
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(match.group(4)),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    season_soup = get_soup(session, SEASON_URL)
    season_start, season_end = season_years(season_soup)
    records = []
    for url in production_urls(season_soup, season_start, season_end):
        try:
            records.extend(
                detail_records(get_soup(session, url), url, season_start, season_end)
            )
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class OperadeOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operade_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperadeOrgCrawler().run()


if __name__ == '__main__':
    main()
