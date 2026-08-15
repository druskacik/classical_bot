import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kolarac.rs/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'category/koncerti/')
SOURCE = 'Zadužbina Ilije M. Kolarca'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sr-RS,sr;q=0.9,en;q=0.7',
}

DATE_RE = re.compile(r'(?<!\d)(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\.?')
TIME_RE = re.compile(r'\b(?:у|u|од|od)\s*(\d{1,2})(?:[.:](\d{2}))?\b', re.IGNORECASE)
PAGE_RE = re.compile(r'/category/koncerti/page/(\d+)/?')
SERBIAN_MONTHS = {
    'јануар': 1, 'januar': 1, 'фебруар': 2, 'februar': 2,
    'март': 3, 'mart': 3, 'април': 4, 'april': 4,
    'мај': 5, 'maj': 5, 'јун': 6, 'jun': 6,
    'јул': 7, 'jul': 7, 'август': 8, 'avgust': 8,
    'септембар': 9, 'septembar': 9, 'октобар': 10, 'oktobar': 10,
    'новембар': 11, 'novembar': 11, 'децембар': 12, 'decembar': 12,
}
TEXT_DATE_RE = re.compile(
    r'(?<!\d)(\d{1,2})\.?(?:\s+)(%s)\b' % '|'.join(SERBIAN_MONTHS),
    re.IGNORECASE,
)
KNOWN_VENUE_RE = re.compile(
    r'\b(Велика дворана|Музички салон|Музичка галерија|Мала сала|'
    r'Velika dvorana|Muzički salon|Muzička galerija|Mala sala)\b',
    re.IGNORECASE,
)


def clean_text(value, separator=' '):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(separator, strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', value).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def archive_page_count(soup):
    pages = [1]
    for link in soup.select('a[href*="/category/koncerti/page/"]'):
        match = PAGE_RE.search(link.get('href', ''))
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def parse_heading(heading):
    parts = [clean_text(part) for part in heading.stripped_strings]
    parts = [part for part in parts if part]
    combined = ' '.join(parts)
    date_match = DATE_RE.search(combined)
    if not date_match:
        return None

    try:
        event_date = date(
            int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1))
        ).isoformat()
    except ValueError:
        return None

    before_date = combined[:date_match.start()].strip(' ,-–—')
    after_date = combined[date_match.end():].strip(' ,-–—')
    time_match = TIME_RE.search(after_date)
    time_from = None
    additional_times = []
    venue = ''
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        if hour < 24 and minute < 60:
            time_from = f'{hour:02d}:{minute:02d}'
        venue = after_date[time_match.end():].strip(' .,-–—')
        additional_match = re.match(
            r'(?:и|i)\s*(\d{1,2})(?:[.:](\d{2}))?\s+', venue, re.IGNORECASE
        )
        if additional_match:
            extra_hour = int(additional_match.group(1))
            extra_minute = int(additional_match.group(2) or 0)
            if extra_hour < 24 and extra_minute < 60:
                additional_times.append(f'{extra_hour:02d}:{extra_minute:02d}')
            venue = venue[additional_match.end():].strip(' .,-–—')

    return {
        'title': before_date,
        'date': event_date,
        'time_from': time_from,
        'additional_times': additional_times,
        'venue': venue,
    }


def article_description(article, heading):
    content = article.select_one('.blog-content')
    if not content:
        return None
    content = BeautifulSoup(str(content), 'html.parser')
    for unwanted in content.select('h3.entry-title, .entry-meta, script, style'):
        unwanted.decompose()
    text = clean_text(content, separator='\n')
    return text or None


def fallback_title(description):
    if not description:
        return ''
    for line in description.splitlines():
        candidate = clean_text(line)
        if candidate and not DATE_RE.search(candidate):
            return candidate
    return ''


def parse_legacy_article(article, description):
    text = description or ''
    date_match = TEXT_DATE_RE.search(text)
    venue_match = KNOWN_VENUE_RE.search(text)
    published = article.select_one('time.entry-date[datetime]')
    if not date_match or not venue_match or not published:
        return None
    year_match = re.match(r'(\d{4})-', published.get('datetime', ''))
    if not year_match:
        return None
    try:
        event_date = date(
            int(year_match.group(1)),
            SERBIAN_MONTHS[date_match.group(2).casefold()],
            int(date_match.group(1)),
        ).isoformat()
    except (KeyError, ValueError):
        return None
    time_match = re.search(
        r'\b(?:у|u|од|od)\s*(\d{1,2})(?:[.:](\d{2}))?\s*(?:сати|h)?\b',
        text[date_match.end():],
        re.IGNORECASE,
    )
    time_from = None
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        if hour < 24 and minute < 60:
            time_from = f'{hour:02d}:{minute:02d}'
    return {'date': event_date, 'time_from': time_from, 'venue': venue_match.group(1)}


def parse_article(article):
    heading = article.select_one('h3.entry-title')
    link = heading.find('a', href=True) if heading else None
    if not heading or not link:
        return None
    url = urljoin(SOURCE_URL, link['href'])
    if urlparse(url).netloc not in {'kolarac.rs', 'www.kolarac.rs'}:
        return None
    description = article_description(article, heading)
    parsed = parse_heading(heading)
    if not parsed or not parsed['venue']:
        parsed = parse_legacy_article(article, description)
    if not parsed or not parsed['venue']:
        return None
    title = parsed.get('title') or 'Концерт'
    if not title:
        return None

    record = {
        'title': title,
        'date': parsed['date'],
        'url': url.replace('http://www.kolarac.rs/', SOURCE_URL),
        'time_from': parsed['time_from'],
        'venue': parsed['venue'],
        'city': 'Belgrade',
        'country_code': 'RS',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    return [record, *[{**record, 'time_from': value}
                      for value in parsed.get('additional_times', [])]]


def parse_archive_page(soup):
    records = []
    for article in soup.select('article.category-koncerti'):
        records.extend(parse_article(article) or [])
    return records


class KolaracRsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kolarac_rs',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RS',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        first_page = fetch_soup(session, ARCHIVE_URL)
        page_count = archive_page_count(first_page)
        records = parse_archive_page(first_page)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(
                    fetch_soup, session, urljoin(ARCHIVE_URL, f'page/{page}/')
                ): page
                for page in range(2, page_count + 1)
            }
            for future in as_completed(futures):
                page = futures[future]
                url = urljoin(ARCHIVE_URL, f'page/{page}/')
                try:
                    records.extend(parse_archive_page(future.result()))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Kolarac concert archive page',
                        event='crawler_page_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    KolaracRsCrawler().run()


if __name__ == '__main__':
    main()
