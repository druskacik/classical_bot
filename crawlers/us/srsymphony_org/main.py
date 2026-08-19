import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.srsymphony.org/'
CALENDAR_URL = f'{SOURCE_URL}event-calendar/'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Santa Rosa Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Weill Hall, Green Music Center': 'Rohnert Park',
    'Luther Burbank Center For The Arts': 'Santa Rosa',
    'Brannan Center': 'Calistoga',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_datetime(value):
    value = clean_text(value)
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
        r'([A-Z][a-z]+ \d{1,2}, \d{4})\s*\|\s*'
        r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        event_time = datetime.strptime(match.group(2).upper(), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None, None
    return event_date, event_time


def description_from_page(soup):
    parts = []
    banner = soup.select_one('.evt_banner_content')
    if banner:
        for paragraph in banner.select('p'):
            text = clean_text(paragraph)
            if len(text) >= 30 and 'ticket' not in text.lower() and text not in parts:
                parts.append(text)

    for section in soup.select('.dt-content'):
        heading = clean_text(section.select_one('h2'))
        if heading.lower() not in {'program', 'artists'}:
            continue
        lines = [clean_text(item) for item in section.select('h5')]
        lines = [line for line in lines if line]
        if lines:
            parts.append(f"{heading}\n" + '\n'.join(lines))
    return '\n\n'.join(parts) or None


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.evt_banner_content h1'))
    if not title:
        return []
    description = description_from_page(soup)
    records = []
    for section in soup.select('.evt_location'):
        section_name = clean_text(section.select_one('h2'))
        if section_name not in {'Performances', 'Discovery Rehearsal'}:
            continue
        for occurrence in section.select(':scope > .loc_info'):
            venue = clean_text(occurrence.select_one('h5'))
            city = VENUE_CITIES.get(venue, '')
            event_date, time_from = parse_datetime(occurrence.select_one('p'))
            if not event_date or not time_from or not venue or not city:
                continue
            record_title = title
            if section_name == 'Discovery Rehearsal':
                record_title = f'{title} — Discovery Rehearsal'
            records.append({
                'title': record_title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def fetch_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_event_page(response.text, url)


def listing_urls(session):
    urls = []
    page = 1
    while True:
        response = session.post(
            AJAX_URL,
            data={
                'page': page,
                'frmData': 'search=&term=&concert_season=',
                'action': 'cvf_event_pagination_load_tribe_events',
            },
            headers={'Referer': CALENDAR_URL, 'X-Requested-With': 'XMLHttpRequest'},
            timeout=45,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = [
            link['href'] for link in soup.select('.event_item .more-detail a.link[href]')
        ]
        if not page_urls:
            break
        for url in page_urls:
            if url not in urls:
                urls.append(url)
        next_link = soup.select_one(".cvf-universal-pagination li.active[p]")
        next_pages = [
            int(item['p']) for item in soup.select('.cvf-universal-pagination li.active[p]')
            if item.get('p', '').isdigit() and int(item['p']) > page
        ]
        if not next_link or not next_pages:
            break
        page = min(next_pages)
    return urls


class SrSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='srsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = listing_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Santa Rosa Symphony event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        if not records:
            log_message(
                'No Santa Rosa Symphony performances found',
                event='crawler_empty_listing',
                level='warning',
                url=CALENDAR_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
        )


def main():
    SrSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
