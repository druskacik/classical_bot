import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nashvilleopera.org/'
SOURCE = 'Nashville Opera'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SEASON_SLUG = 'season'
CITY = 'Nashville'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

PERFORMANCE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\.?\s+\d{1,2},\s+\d{4})\s*[–—-]\s*'
    r'(\d{1,2}(?::\d{2})?\s*[AP]M)$',
    re.IGNORECASE,
)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = value.replace('.', '')
    for pattern in ('%b %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value.upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def api_page(session, slug):
    response = session.get(
        API_URL,
        params={'slug': slug, '_fields': 'link,title,content'},
        timeout=45,
    )
    response.raise_for_status()
    pages = response.json()
    return pages[0] if pages else None


def season_detail_slugs(season_page):
    soup = BeautifulSoup(season_page['content']['rendered'], 'html.parser')
    slugs = []
    for link in soup.select('a[href]'):
        parsed = urlparse(link.get('href', ''))
        if parsed.netloc not in ('', 'nashvilleopera.org', 'www.nashvilleopera.org'):
            continue
        slug = parsed.path.strip('/').split('/')[-1]
        if slug and slug not in {SEASON_SLUG, 'season-packages'} and slug not in slugs:
            slugs.append(slug)
    return slugs


def records_from_page(page):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    text = clean_text(soup.get_text('\n', strip=True))
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    try:
        section_start = lines.index('Performance Dates') + 1
    except ValueError:
        return []

    title = clean_text(BeautifulSoup(page['title']['rendered'], 'html.parser').get_text())
    venue = lines[section_start] if section_start < len(lines) else ''
    if not title or not venue:
        return []

    records = []
    for line in lines[section_start + 1:]:
        if line.lower().startswith(('buy tickets', 'subscribe')):
            break
        match = PERFORMANCE_RE.match(line)
        if not match:
            continue
        event_date = parse_date(match.group(1))
        time_from = parse_time(match.group(2))
        if not event_date or not time_from:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': page['link'],
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': text or None,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    season_page = api_page(session, SEASON_SLUG)
    if not season_page:
        log_message(
            'Season page not found',
            event='crawler_empty_listing',
            level='warning',
            url=f'{SOURCE_URL}{SEASON_SLUG}/',
            record_count=0,
        )
        return []

    records = []
    for slug in season_detail_slugs(season_page):
        try:
            page = api_page(session, slug)
            if page:
                records.extend(records_from_page(page))
        except (requests.RequestException, ValueError, KeyError) as error:
            log_message(
                'Could not parse season production',
                event='crawler_detail_failed',
                level='warning',
                url=f'{SOURCE_URL}{slug}/',
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No performance dates found',
            event='crawler_empty_listing',
            level='warning',
            url=f'{SOURCE_URL}{SEASON_SLUG}/',
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class NashvilleOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nashvilleopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    NashvilleOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
