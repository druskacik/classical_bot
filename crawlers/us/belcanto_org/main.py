import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.belcanto.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts')
SENIOR_CONCERTS_PATH = '/concerts-1'
SOURCE = 'Bel Canto Chorus'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})\s*\|\s*'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))',
    re.IGNORECASE,
)
SENIOR_DATE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2}),\s*(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    normalized = re.sub(r'\.', '', clean_text(value)).upper()
    for pattern in ('%I:%M %p', '%I:%M%p', '%I%p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def html_blocks(soup):
    return [
        clean_text(node)
        for node in soup.select('.sqs-html-content')
        if clean_text(node)
    ]


def description_from_blocks(blocks):
    start = next((index for index, text in enumerate(blocks) if text == 'The Program'), None)
    if start is None:
        return None
    parts = []
    for text in blocks[start + 1:]:
        if DATE_TIME_RE.search(text):
            break
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    blocks = html_blocks(soup)
    title_node = soup.select_one('meta[property="og:title"]')
    title = clean_text(title_node.get('content')) if title_node else ''
    if title.endswith(' — Bel Canto Chorus'):
        title = title.removesuffix(' — Bel Canto Chorus').strip()

    records = []
    for block in blocks:
        matches = list(DATE_TIME_RE.finditer(block))
        if not matches:
            continue
        lines = [line for line in block.splitlines() if line]
        last_match_line = max(
            index for index, line in enumerate(lines) if DATE_TIME_RE.search(line)
        )
        venue = lines[last_match_line + 1] if last_match_line + 1 < len(lines) else ''
        address = '\n'.join(lines[last_match_line + 2:])
        city_match = re.search(r'(?:,\s*|\n)([A-Za-z][A-Za-z .\'-]+),\s*WI\b', address)
        city = city_match.group(1).strip() if city_match else ''
        if not title or not venue or not city:
            continue

        for match in matches:
            try:
                event_date = datetime.strptime(
                    f"{match.group('month')} {match.group('day')} {match.group('year')}",
                    '%B %d %Y',
                ).date().isoformat()
            except ValueError:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(match.group('time')),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description_from_blocks(blocks),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
        break
    return records


def parse_senior_concerts(html, url, season_start_year):
    soup = BeautifulSoup(html, 'html.parser')
    blocks = html_blocks(soup)
    description = next(
        (block for block in blocks if 'Bel Canto Senior Singers are open' in block),
        None,
    )
    records = []
    for block in blocks:
        match = SENIOR_DATE_RE.search(block)
        if not match:
            continue
        lines = [line for line in block.splitlines() if line]
        date_index = next(
            (index for index, line in enumerate(lines) if SENIOR_DATE_RE.fullmatch(line)),
            None,
        )
        if date_index is None or date_index + 2 >= len(lines):
            continue
        venue = lines[date_index + 1]
        address = lines[date_index + 2]
        city_match = re.search(r',\s*([A-Za-z][A-Za-z .\'-]+)(?:,\s*WI)?$', address)
        city = city_match.group(1).strip() if city_match else ''
        if not venue or not city:
            continue
        try:
            event_date = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {season_start_year}",
                '%B %d %Y',
            ).date().isoformat()
        except ValueError:
            continue
        group = lines[0] if lines else 'Bel Canto Senior Singers'
        records.append({
            'title': group,
            'date': event_date,
            'url': url,
            'time_from': parse_time(match.group('time')),
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
    soup = BeautifulSoup(response.text, 'html.parser')

    detail_urls = {
        urljoin(LISTING_URL, anchor.get('href'))
        for anchor in soup.find_all('a', href=True)
        if clean_text(anchor) == 'Concert Details'
    }
    detail_urls = {
        url for url in detail_urls
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc
    }

    records = []
    for url in sorted(detail_urls):
        try:
            detail = session.get(url, timeout=45)
            detail.raise_for_status()
            records.extend(parse_detail_page(detail.text, url))
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    season_match = re.search(r'\b(\d{2})-(\d{2}) Season\b', clean_text(soup))
    senior_url = urljoin(SOURCE_URL, SENIOR_CONCERTS_PATH)
    if season_match:
        try:
            senior = session.get(senior_url, timeout=45)
            senior.raise_for_status()
            records.extend(
                parse_senior_concerts(senior.text, senior_url, 2000 + int(season_match.group(1)))
            )
        except requests.RequestException as error:
            log_message(
                'Senior concert request failed',
                event='crawler_detail_request_failed',
                level='warning',
                url=senior_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BelcantoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='belcanto_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BelcantoOrgCrawler().run()


if __name__ == '__main__':
    main()
