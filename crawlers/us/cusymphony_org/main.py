import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cusymphony.org/'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Champaign-Urbana Symphony Orchestra'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*\|\s*'
    r'(\d{1,2}:\d{2})\s*([ap])\.?m\.?',
    re.IGNORECASE,
)
SEASON_SLUG_RE = re.compile(r'^(\d{4})-\d{2}-season$')


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            PAGES_API,
            params={'per_page': 10, 'page': page_number, 'orderby': 'date', 'order': 'desc'},
            timeout=45,
        )
        if response.status_code == 400 and pages:
            break
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page_number))
        if page_number >= total_pages:
            break
        page_number += 1
    return pages


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        event_time = datetime.strptime(
            f'{match.group(2)} {match.group(3)}m', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None
    return event_date, event_time


def normalized_title(value):
    return re.sub(r'[^a-z0-9]+', '', clean_text(value).lower())


def page_title(page):
    return clean_text((page.get('title') or {}).get('rendered'))


def page_content(page):
    return (page.get('content') or {}).get('rendered') or ''


def resolve_location(body_text):
    lower = body_text.lower()
    if 'faith united methodist church' in lower:
        return 'Faith United Methodist Church', 'Champaign'
    if 'foellinger great hall' in lower:
        return 'Foellinger Great Hall, Krannert Center for the Performing Arts', 'Urbana'
    if 'krannert center for the performing arts' in lower:
        return 'Krannert Center for the Performing Arts', 'Urbana'
    return None, None


def season_records(season_page, pages):
    soup = BeautifulSoup(page_content(season_page), 'html.parser')
    title_urls = {
        normalized_title(page_title(page)): page.get('link')
        for page in pages
        if page_title(page) and page.get('link')
    }
    records = []
    for item in soup.select('.aagb__accordion_container'):
        head = item.select_one('.aagb__accordion_head')
        body = item.select_one('.aagb__accordion_body')
        if not head or not body:
            continue
        head_text = clean_text(head)
        parsed = parse_date_time(head_text)
        date_match = DATE_TIME_RE.search(head_text)
        title = clean_text(head_text[:date_match.start()]) if date_match else ''
        body_text = clean_text(body, separator='\n')
        venue, city = resolve_location(body_text)
        if not title or not parsed or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': parsed[0],
            'url': title_urls.get(normalized_title(title), season_page['link']),
            'time_from': parsed[1],
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': body_text or None,
        })
    return records


def youth_records(page):
    soup = BeautifulSoup(page_content(page), 'html.parser')
    text = clean_text(soup, separator='\n')
    venue, city = resolve_location(text)
    if not venue or not city:
        return []

    title = 'CUSO Youth Concerts: We’ve Got Rhythm!'
    records = []
    for match in DATE_TIME_RE.finditer(text):
        parsed = parse_date_time(match.group(0))
        if parsed:
            records.append({
                'title': title,
                'date': parsed[0],
                'url': page['link'],
                'time_from': parsed[1],
                'venue': venue,
                'city': city,
                'country_code': COUNTRY_CODE,
                'description': text or None,
            })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    pages = get_pages(session)

    season_pages = []
    for page in pages:
        match = SEASON_SLUG_RE.fullmatch(page.get('slug') or '')
        if match:
            season_pages.append((int(match.group(1)), page))
    if not season_pages:
        log_message(
            'No season page found',
            event='crawler_source_empty',
            level='warning',
            url=PAGES_API,
            record_count=0,
        )
        return []

    _, current_season = max(season_pages, key=lambda item: item[0])
    records = season_records(current_season, pages)
    youth_page = next((page for page in pages if page.get('slug') == 'youth-concerts'), None)
    if youth_page:
        records.extend(youth_records(youth_page))

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class CuSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cusymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CuSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
