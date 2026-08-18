import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hhso.org/'
ARCHIVE_URL = f'{SOURCE_URL}?post_type=event'
SOURCE = 'Hilton Head Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;q=0.9,'
        'image/avif,image/webp,*/*;q=0.8'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_FORMATS = ('%B %d, %Y', '%b %d, %Y')
DEFAULT_CITY = 'Hilton Head Island'
LOCAL_VENUES = {
    'soundwaves': 'SoundWaves',
    'first presbyterian church': 'First Presbyterian Church',
    'lowcountry celebration park': 'Lowcountry Celebration Park',
    'coastal discovery museum': 'Coastal Discovery Museum',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    lines = [re.sub(r'\s+', ' ', line).strip(' ,') for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def parse_date(value):
    value = clean_text(value).replace('\n', ' ')
    match = re.search(r'[A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?, \d{4}', value, re.I)
    if not match:
        return None
    value = match.group(0)
    value = re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', value, flags=re.I)
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_location(value):
    lines = [line for line in clean_text(value).splitlines() if line]
    if not lines:
        return None, None

    if len(lines) >= 2:
        venue = lines[0]
        city = lines[-1]
    elif ',' in lines[0]:
        venue, city = [part.strip() for part in lines[0].rsplit(',', 1)]
    else:
        venue = lines[0]
        city = DEFAULT_CITY

    venue = re.sub(r'\s+at\s+Coligny$', '', venue, flags=re.I).strip()
    venue_key = venue.lower()
    for fragment, canonical in LOCAL_VENUES.items():
        if fragment in venue_key:
            venue = canonical
            break

    city = re.sub(r'\bSC\b.*$', '', city, flags=re.I).strip(' ,')
    if city.lower() in {'hilton head', 'hilton head island'}:
        city = DEFAULT_CITY
    if not venue or not city or venue.casefold() == city.casefold():
        return None, None
    return venue, city


def archive_page_url(page_number):
    if page_number == 1:
        return ARCHIVE_URL
    return f'{SOURCE_URL}page/{page_number}/?post_type=event'


def event_links_from_archive(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for anchor in soup.select('a[href*="/event/"]'):
        url = urljoin(SOURCE_URL, anchor.get('href'))
        parsed = urlparse(url)
        if parsed.netloc == 'www.hhso.org' and parsed.path != '/event/':
            links.add(f'{parsed.scheme}://{parsed.netloc}{parsed.path}')

    page_numbers = []
    for anchor in soup.select('a[href*="post_type=event"]'):
        match = re.search(r'/page/(\d+)/', anchor.get('href', ''))
        if match:
            page_numbers.append(int(match.group(1)))
    return links, max(page_numbers, default=1)


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.post-content')
    info_items = article.select('.info-table li') if article else []
    if len(info_items) < 3:
        return None

    date_value = time_value = location_value = ''
    for item in info_items:
        classes = ' '.join(icon.get('class', [])) if (icon := item.find('i')) else ''
        value = clean_text(item)
        if 'calendar' in classes:
            date_value = value
        elif 'clock' in classes:
            time_value = value
        elif 'map-marker' in classes:
            location_value = value

    event_date = parse_date(date_value)
    venue, city = parse_location(location_value)
    title_node = soup.select_one('h2.post-title')
    title = clean_text(title_node)
    description_node = article.select_one('.page-content') if article else None
    description = clean_text(description_node) or None

    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_value),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_html(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    first_html = get_html(session, ARCHIVE_URL)
    event_links, last_page = event_links_from_archive(first_html)
    for page_number in range(2, last_page + 1):
        html = get_html(session, archive_page_url(page_number))
        page_links, _ = event_links_from_archive(html)
        event_links.update(page_links)

    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_html, session, url): url for url in event_links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event_page(future.result(), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=ARCHIVE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class HhsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hhso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
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
        return scrape_concerts()


def main():
    HhsoOrgCrawler().run()


if __name__ == '__main__':
    main()
