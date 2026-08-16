import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chambermusicreading.org/'
SOURCE = 'Friends of Chamber Music Reading'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
DEFAULT_VENUE = 'WCR Center for the Arts'
DEFAULT_CITY = 'Reading'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

DATE_PATTERN = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+(\d{1,2}),\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_PATTERN = re.compile(
    r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?\s*M\.?',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    soup = value if hasattr(value, 'get_text') else BeautifulSoup(value, 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_PATTERN.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'P':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def venue_from_text(text):
    if re.search(r'Trinity Lutheran Church', text, re.IGNORECASE):
        return 'Trinity Lutheran Church'
    return DEFAULT_VENUE


def is_season_overview(title):
    return bool(re.search(r'\b20\d{2}\s*[\u2013\u2014-]\s*(?:20)?\d{2}\s+season\b', title, re.I))


def make_record(title, event_date, url, text):
    title = html.unescape(clean_text(title))
    if not title or not event_date or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(text),
        'venue': venue_from_text(text),
        'city': DEFAULT_CITY,
        'country_code': 'US',
        'description': text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_event_posts(posts, detail_html_by_url):
    records = []
    event_urls = {}
    for post in posts:
        title = clean_text(post.get('title', {}).get('rendered'))
        url = post.get('link')
        content = clean_text(post.get('content', {}).get('rendered'))
        content_date = parse_date(content)
        if title and url and not is_season_overview(title) and not content_date:
            event_urls[title.casefold()] = url

        detail_soup = BeautifulSoup(detail_html_by_url.get(url, ''), 'html.parser')
        start = detail_soup.select_one(
            '.sc-frontend-single-event__details__val-date time[datetime]'
        )
        if start is None:
            start = detail_soup.select_one('time[datetime]')
        start_value = start.get('datetime', '') if start else ''
        event_date = start_value[:10] if re.fullmatch(r'\d{4}-\d{2}-\d{2}.*', start_value) else content_date
        if not event_date or is_season_overview(title):
            continue
        record = make_record(title, event_date, url, content)
        if record:
            displayed_time = detail_soup.select_one(
                '.sc-frontend-single-event__details__time time'
            )
            if displayed_time is not None:
                record['time_from'] = parse_time(clean_text(displayed_time)) or record['time_from']
            location = clean_text(detail_soup.select_one(
                '.sc-frontend-single-event__details__location '
                '.sc-frontend-single-event__details__val'
            ))
            if location:
                record['venue'] = re.split(r'[,\n]', location, maxsplit=1)[0].strip() or record['venue']
            records.append(record)
    return records, event_urls


def parse_season_page(page, event_urls):
    records = []
    soup = BeautifulSoup(page.get('content', {}).get('rendered', ''), 'html.parser')
    page_url = page.get('link') or SOURCE_URL
    full_text = clean_text(str(soup))
    for heading in soup.select('h2, h3, h4, p'):
        text = clean_text(str(heading))
        event_date = parse_date(text)
        if not event_date:
            continue
        strong = heading.find('strong')
        title = clean_text(str(strong)) if strong else DATE_PATTERN.sub('', text)
        title = title.strip(' :\u2013\u2014-*')
        url = event_urls.get(title.casefold(), page_url)
        record = make_record(title, event_date, url, text)
        if record:
            if (
                event_date.endswith('-05-16')
                and re.search(r'May 16(?:th)? concert.*Trinity Lutheran Church', full_text, re.I)
            ):
                record['venue'] = 'Trinity Lutheran Church'
            records.append(record)
    return records


class ChamberMusicReadingOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicreading_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
            posts = []
            page_number = 1
            while True:
                event_response = session.get(
                    f'{API_URL}/sc_event',
                    params={
                        'per_page': 100,
                        'page': page_number,
                        'orderby': 'date',
                        'order': 'desc',
                        '_fields': 'title,link,content',
                    },
                    timeout=45,
                )
                event_response.raise_for_status()
                posts.extend(event_response.json())
                total_pages = int(event_response.headers.get('X-WP-TotalPages', '1'))
                if page_number >= total_pages:
                    break
                page_number += 1
            page_response = session.get(
                f'{API_URL}/pages',
                params={
                    'slug': '2025-26-season',
                    '_fields': 'link,content',
                },
                timeout=45,
            )
            page_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Friends of Chamber Music Reading events',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        detail_html_by_url = {}
        for post in posts:
            url = post.get('link')
            if not url or is_season_overview(clean_text(post.get('title', {}).get('rendered'))):
                continue
            try:
                for attempt in range(3):
                    response = session.get(url, timeout=45)
                    response.raise_for_status()
                    detail_html_by_url[url] = response.text
                    if BeautifulSoup(response.text, 'html.parser').select_one(
                        '.sc-frontend-single-event__details__val-date time[datetime]'
                    ):
                        break
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch concert detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records, event_urls = parse_event_posts(posts, detail_html_by_url)
        pages = page_response.json()
        if pages:
            existing = {(record['title'].casefold(), record['date']) for record in records}
            records.extend(
                record for record in parse_season_page(pages[0], event_urls)
                if (record['title'].casefold(), record['date']) not in existing
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ChamberMusicReadingOrgCrawler().run()


if __name__ == '__main__':
    main()
