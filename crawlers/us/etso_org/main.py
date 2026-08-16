import html
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://etso.org/'
SOURCE = 'East Texas Symphony Orchestra'
PAGES_API = urljoin(SOURCE_URL, 'wp-json/wp/v2/pages')
CITY = 'Tyler'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH_PATTERN = (
    r'January|February|March|April|May|June|July|August|September|October|'
    r'November|December'
)
DATE_PATTERN = rf'(?:{MONTH_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d{{2}}'
TIME_PATTERN = r'\d{1,2}:\d{2}\s*(?:AM|PM|a\.m\.|p\.m\.)'
OVERVIEW_SLUGS = {'home', 'season-tickets'}


def clean_text(value):
    if not value:
        return ''
    value = html.unescape(str(value))
    value = re.sub(r'\[/?et_pb[^\]]*\]', ' ', value)
    text = BeautifulSoup(value, 'html.parser').get_text(' ', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    value = re.sub(r'(\d)(?:st|nd|rd|th)', r'\1', value, flags=re.I)
    value = value.replace(',', '')
    try:
        return datetime.strptime(value.title(), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    normalized = value.lower().replace('.', '').replace(' ', '')
    try:
        return datetime.strptime(normalized, '%I:%M%p').strftime('%H:%M')
    except ValueError:
        return None


def content_lines(rendered):
    soup = BeautifulSoup(html.unescape(rendered or ''), 'html.parser')
    lines = []
    for element in soup.select('p, h1, h2, h3, li'):
        text = clean_text(element)
        if text and text not in lines:
            lines.append(text)
    return lines


def occurrence(lines):
    pattern = re.compile(
        rf'({DATE_PATTERN})\s*\|\s*({TIME_PATTERN})\s*\|\s*(.+)$', re.I
    )
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        event_date = parse_date(match.group(1))
        time_from = parse_time(match.group(2))
        venue = match.group(3).strip(' -–—|')
        if event_date and time_from and venue:
            return event_date, time_from, venue
    return None


def description_from(lines, title):
    excluded = {
        title.lower(),
        'southside bank is proud to be a season sponsor',
    }
    useful = []
    for line in lines:
        lower = line.lower()
        if lower in excluded or re.fullmatch(rf'{DATE_PATTERN}\s*\|.*', line, re.I):
            continue
        if any(term in lower for term in ('purchase tickets', 'order your tickets')):
            continue
        useful.append(line)
    return '\n\n'.join(useful) or None


def make_record(page):
    slug = page.get('slug') or ''
    if slug in OVERVIEW_SLUGS or re.fullmatch(r'\d{2}-\d{2}-season', slug):
        return None

    title = clean_text((page.get('title') or {}).get('rendered'))
    url = page.get('link') or ''
    lines = content_lines((page.get('content') or {}).get('rendered'))
    parsed = occurrence(lines)
    if not title or not url or not parsed:
        return None

    event_date, time_from, venue = parsed
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description_from(lines, title),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            PAGES_API,
            params={
                'per_page': 100,
                'page': page_number,
                '_fields': 'id,slug,link,title,content',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page_number >= total_pages:
            return pages
        page_number += 1


def season_fallback_records(pages):
    """Recover concrete season occurrences whose former detail page is now gone."""
    records = []
    card_pattern = re.compile(rf'({DATE_PATTERN})', re.I)
    for page in pages:
        if not re.fullmatch(r'\d{2}-\d{2}-season', page.get('slug') or ''):
            continue
        rendered = html.unescape((page.get('content') or {}).get('rendered') or '')
        columns = re.findall(
            r'\[et_pb_column\b[^\]]*\](.*?)\[/et_pb_column\]', rendered, re.I | re.S
        )
        for column in columns:
            url_match = re.search(
                r'\[et_pb_image\b[^\]]*\burl=["“”](https?://[^"“”\s]+)', column, re.I
            )
            if not url_match:
                continue
            href = urljoin(SOURCE_URL, url_match.group(1))
            if urlparse(href).netloc not in {'etso.org', 'www.etso.org'}:
                continue
            soup = BeautifulSoup(column, 'html.parser')
            text = clean_text(column)
            match = card_pattern.search(text)
            title = clean_text(soup.select_one('h1, h2, h3'))
            if not match or not title:
                continue
            event_date = parse_date(match.group(1))
            if not event_date:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': page.get('link'),
                'time_from': None,
                'venue': 'UT Tyler Cowan Center',
                'city': CITY,
                'country_code': 'US',
                'description': None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
                '_detail_url': href,
            })
    return records


class EtsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='etso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        try:
            pages = get_pages(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch ETSO pages',
                event='crawler_fetch_failed',
                level='error',
                url=PAGES_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = [record for page in pages if (record := make_record(page))]
        detail_urls = {record['url'].rstrip('/') for record in records}
        detail_titles = {record['title'].casefold() for record in records}
        for record in season_fallback_records(pages):
            detail_url = record.pop('_detail_url').rstrip('/')
            if detail_url in detail_urls or record['title'].casefold() in detail_titles:
                continue
            records.append(record)
            detail_titles.add(record['title'].casefold())

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    EtsoOrgCrawler().run()


if __name__ == '__main__':
    main()
