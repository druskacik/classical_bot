import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://clubmusicaldequebec.com/cmq/'
PROGRAMMATION_URL = urljoin(SOURCE_URL, 'index.php/programmation')
SOURCE = 'Club musical de Québec'
VENUE_CITY = 'Québec'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CA,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
    'decembre': 12,
}

DATE_RE = re.compile(
    r'\b(\d{1,2})\s+'
    r'(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|'
    r'septembre|octobre|novembre|décembre|decembre)\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b([01]?\d|2[0-3])\s*h(?:\s*([0-5]\d))?\b', re.IGNORECASE)
DETAIL_PATH_RE = re.compile(
    r'/programmation/(?:saison-\d{4}-\d{4}|eclat-[^/]+)/[^/]+$'
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return datetime(int(year), MONTHS[month.lower()], int(day)).date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def detail_urls(soup):
    urls = set()
    for link in soup.select('.sp-megamenu-parent a[href]'):
        url = urljoin(PROGRAMMATION_URL, link.get('href')).rstrip('/')
        if DETAIL_PATH_RE.search(urlparse(url).path):
            urls.add(url)
    return sorted(urls)


def make_record(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.select_one('[itemprop="articleBody"]')
    if body is None:
        return None

    lines = [line for line in clean_text(body).splitlines() if line]
    date_index = next((i for i, line in enumerate(lines) if parse_date(line)), None)
    heading = body.select_one('h1')
    title = clean_text(heading).split('|', 1)[0].strip() if heading else ''
    if not title and lines:
        title = lines[0].split('|', 1)[0].strip()
    if date_index is None:
        return None

    date_line = lines[date_index]
    date = parse_date(date_line)
    time_from = parse_time(date_line)
    venue = ''
    for line in lines[date_index + 1:date_index + 5]:
        if time_from is None:
            time_from = parse_time(line)
        if not venue and 'palais montcalm' in line.lower():
            venue = line

    if not title or not date or not venue:
        return None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': VENUE_CITY,
        'country_code': 'CA',
        'description': clean_text(body) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ClubmusicaldequebecComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='clubmusicaldequebec_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(PROGRAMMATION_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch programming page',
                event='crawler_page_failed',
                level='error',
                url=PROGRAMMATION_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in detail_urls(BeautifulSoup(response.text, 'html.parser')):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            record = make_record(detail_response.url.rstrip('/'), detail_response.text)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped concert with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ClubmusicaldequebecComCrawler().run()


if __name__ == '__main__':
    main()
