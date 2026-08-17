from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.siouxcitysymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Sioux City Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    return '\n'.join(line.strip() for line in text.splitlines() if line.strip())


def get_page(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def event_urls(session):
    soup = BeautifulSoup(get_page(session, SITEMAP_URL), 'xml')
    urls = []
    for location in soup.find_all('loc'):
        url = clean_text(location)
        parsed = urlparse(url)
        if parsed.netloc == 'www.siouxcitysymphony.org' and parsed.path.startswith('/event/'):
            urls.append(url)
    return sorted(set(urls))


def make_description(soup):
    parts = []
    about = clean_text(soup.select_one('.about-event-description'))
    if about:
        parts.append(about)
    programme = clean_text(soup.select_one('.event-program-body'))
    if programme:
        parts.append(f'Program\n{programme}')
    return '\n\n'.join(parts) or None


def make_record(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.events-details-title'))
    venue = clean_text(
        soup.select_one('.events-details-location .without-bottom-spacing')
    )
    location_text = clean_text(soup.select_one('.events-details-location'))
    city = 'Sioux City' if 'Sioux City' in location_text else ''
    date_parts = soup.select('.events-location-date .without-bottom-spacing')
    date_text = clean_text(date_parts[0]) if date_parts else ''
    time_text = clean_text(date_parts[1]) if len(date_parts) > 1 else ''

    try:
        event_date = datetime.strptime(date_text, '%A, %B %d, %Y').date().isoformat()
        time_from = datetime.strptime(time_text.upper(), '%I:%M %p').strftime('%H:%M')
    except (TypeError, ValueError):
        return None

    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': make_description(soup),
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_page, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = make_record(url, future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Sioux City Symphony event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped invalid Sioux City Symphony event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class SiouxCitySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='siouxcitysymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
    SiouxCitySymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
