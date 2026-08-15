import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://calperformances.org/'
SOURCE = 'Cal Performances'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/cp_event'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', value).strip()


def parse_start(value):
    value = clean_text(value).replace('.', '')
    for pattern in ('%m/%d/%Y %I:%M %p', '%m/%d/%Y %H:%M'):
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            continue
    return None, None


def event_description(soup):
    tabs = soup.select_one('.fusion-tabs .tab-content')
    if tabs:
        description = clean_text(tabs)
    else:
        about_heading = soup.find(
            lambda tag: tag.name in ('h2', 'h3', 'h4')
            and clean_text(tag).lower() == 'about this performance'
        )
        description = clean_text(about_heading.find_parent()) if about_heading else ''
    return description or None


def event_city(venue):
    match = re.search(r',\s*(Oakland|Berkeley)\s*$', venue, re.IGNORECASE)
    return match.group(1).title() if match else 'Berkeley'


def parse_event(event):
    content = event.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(BeautifulSoup(event.get('title', {}).get('rendered', ''), 'html.parser'))
    url = event.get('link', '')
    description = event_description(soup)
    records = []
    seen = set()

    for calendar in soup.select('.addeventatc'):
        start = calendar.select_one('.start')
        location = calendar.select_one('.location')
        event_date, time_from = parse_start(clean_text(start))
        venue = clean_text(location)
        key = (event_date, time_from, venue)
        if not all((title, event_date, url, venue)) or key in seen:
            continue
        seen.add(key)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': event_city(venue),
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class CalPerformancesOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='calperformances_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        skipped_count = 0
        page = 1

        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 20,
                        'page': page,
                        'orderby': 'date',
                        'order': 'desc',
                        '_fields': 'id,link,title,content',
                    },
                    timeout=60,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Cal Performances events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = response.json()
            if not events:
                break
            for event in events:
                parsed = parse_event(event)
                if not parsed:
                    skipped_count += 1
                records.extend(parsed)

            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        if skipped_count:
            log_message(
                'Skipped Cal Performances pages without a complete occurrence',
                event='crawler_records_skipped',
                level='warning',
                record_count=skipped_count,
                url=API_URL,
            )
        return records


def main():
    return CalPerformancesOrgCrawler().run()


if __name__ == '__main__':
    main()
