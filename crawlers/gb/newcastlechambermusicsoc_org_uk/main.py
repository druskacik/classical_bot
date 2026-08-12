import re
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.newcastlechambermusicsoc.org.uk/'
SOURCE = 'Newcastle upon Tyne Chamber Music Society'
VENUE = 'Sage Two at The Glasshouse ICM'
CITY = 'Newcastle upon Tyne'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(
    rf'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    rf'(\d{{1,2}})\s*(?:st|nd|rd|th)?\s+({MONTHS})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'(?<![\d:])(1[0-2]|0?[1-9])(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE
)
POSITION_RE = re.compile(r'\b(left|top):(\d+)px')


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\ufeff', '')
    return re.sub(r'\s+', ' ', value).strip()


def position(node):
    values = {name: int(number) for name, number in POSITION_RE.findall(node.get('style', ''))}
    return values.get('left'), values.get('top')


def season_years(soup, page_url):
    candidates = [soup.title.get_text(' ', strip=True) if soup.title else '', unquote(page_url)]
    match = re.search(r'(20\d{2})\s*-\s*(20\d{2})', ' '.join(candidates))
    return (int(match.group(1)), int(match.group(2))) if match else None


def event_headers(soup):
    headers = []
    for node in soup.select('div[style*="position:absolute"]'):
        text = clean_text(node)
        match = DATE_RE.search(text)
        left, top = position(node)
        paragraphs = [clean_text(p) for p in node.find_all('p', recursive=False) if clean_text(p)]
        if not match or left is None or top is None or len(paragraphs) < 2:
            continue
        title = clean_text(node.select_one('a[href]')) or paragraphs[-1]
        if title and not DATE_RE.search(title):
            headers.append((node, left, top, match, title))
    return headers


def programme_for(header, headers, positioned):
    _, left, top, _, _ = header
    same_column_next = [item[2] for item in headers if abs(item[1] - left) < 180 and item[2] > top]
    bottom = min(same_column_next, default=10_000)
    parts = []
    for node, node_left, node_top in positioned:
        if node is header[0] or not (top < node_top < bottom):
            continue
        if left - 10 <= node_left <= left + 440:
            text = clean_text(node)
            if (
                text and not DATE_RE.search(text) and 'Photo©' not in text
                and not re.fullmatch(r'20\d{2}', text)
            ):
                parts.append(text)
    return ' '.join(dict.fromkeys(parts))


def parse_season(soup, page_url):
    years = season_years(soup, page_url)
    if not years:
        return []
    headers = event_headers(soup)
    positioned = []
    for node in soup.select('div[style*="position:absolute"]'):
        left, top = position(node)
        if left is not None and top is not None:
            positioned.append((node, left, top))

    records = []
    for header in headers:
        node, _, _, date_match, title = header
        day = int(date_match.group(1))
        month = date_match.group(2).title()
        year = years[0] if month in {'August', 'September', 'October', 'November', 'December'} else years[1]
        try:
            date = datetime.strptime(f'{day} {month} {year}', '%d %B %Y').date().isoformat()
        except ValueError:
            continue
        header_text = clean_text(node)
        time_match = TIME_RE.search(header_text)
        time_from = None
        if time_match:
            raw_time = f'{time_match.group(1)}:{time_match.group(2) or "00"}{time_match.group(3)}'
            time_from = datetime.strptime(raw_time.upper(), '%I:%M%p').strftime('%H:%M')
        programme = programme_for(header, headers, positioned)
        description = f'Programme: {programme}' if programme else None
        records.append({
            'title': title,
            'date': date,
            'url': page_url,
            'time_from': time_from,
            'venue': VENUE,
            'city': CITY,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class NewcastleChamberMusicSocOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='newcastlechambermusicsoc_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def fetch(self, session, url):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        response.encoding = 'utf-8'
        return BeautifulSoup(response.text, 'html.parser')

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        home = self.fetch(session, SOURCE_URL)
        queue = []
        for anchor in home.select('a[href]'):
            url = urljoin(SOURCE_URL, anchor.get('href', '')).split('#', 1)[0]
            if 'season' in unquote(urlparse(url).path).lower():
                queue.append(url)

        records = []
        seen = set()
        while queue:
            page_url = queue.pop(0)
            if page_url in seen:
                continue
            seen.add(page_url)
            try:
                soup = self.fetch(session, page_url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Newcastle Chamber Music Society season',
                    event='crawler_item_failed', level='warning', url=page_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            records.extend(parse_season(soup, page_url))
            for anchor in soup.select('a[href]'):
                url = urljoin(page_url, anchor.get('href', '')).split('#', 1)[0]
                if (
                    urlparse(url).netloc == urlparse(SOURCE_URL).netloc
                    and 'season' in unquote(urlparse(url).path).lower()
                    and url not in seen
                ):
                    queue.append(url)
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    NewcastleChamberMusicSocOrgUkCrawler().run()


if __name__ == '__main__':
    main()
