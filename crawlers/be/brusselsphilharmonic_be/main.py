import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brusselsphilharmonic.be/'
CALENDAR_URL = urljoin(SOURCE_URL, 'nl/concerten')
SOURCE = 'Brussels Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-BE,nl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def is_event_url(url):
    path = urlparse(url).path.rstrip('/')
    final = path.rsplit('/', 1)[-1]
    return '/nl/concerten/' in path and not re.fullmatch(r'p\d+', final)


def listing_urls(session):
    first = get_soup(session, CALENDAR_URL)
    page_numbers = [1]
    for link in first.select('a[href*="/nl/concerten/p"]'):
        match = re.search(r'/p(\d+)/?$', link.get('href', ''))
        if match:
            page_numbers.append(int(match.group(1)))

    soups = [first]
    for page_number in range(2, max(page_numbers) + 1):
        soups.append(get_soup(session, f'{CALENDAR_URL}/p{page_number}'))

    urls = set()
    for soup in soups:
        for link in soup.select('a[href*="/nl/concerten/"]'):
            url = urljoin(SOURCE_URL, link.get('href', ''))
            if is_event_url(url):
                urls.add(url)
    return sorted(urls)


def page_title(soup):
    headings = [clean_text(node) for node in soup.select('h1')]
    headings = [
        heading for heading in headings
        if heading.lower() not in {'deze website gebruikt cookies', 'cookie-instellingen'}
    ]
    return headings[0] if headings else ''


def make_record(url, soup):
    holder = soup.select_one('.event__detail__holder')
    time_node = holder.select_one('time[datetime]') if holder else None
    city_node = holder.select_one('.event__city') if holder else None
    venue_node = holder.select_one('.event__venue') if holder else None
    title = page_title(soup)
    city = clean_text(city_node)
    venue = clean_text(venue_node)
    country_code = 'BE'
    country_match = re.search(r'\s*\((NL|DE|AT)\)$', city)
    if country_match:
        country_code = country_match.group(1)
        city = city[:country_match.start()].strip()

    raw_date = time_node.get('datetime', '')[:10] if time_node else ''
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'\b([01]\d|2[0-3]):([0-5]\d)\b', clean_text(time_node))
    if not title or not city or not venue:
        return None

    description_parts = []
    for selector in ('.js-read-more-full', '.event__detail__program'):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_detail(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return make_record(url, get_soup(session, url))


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class BrusselsPhilharmonicBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brusselsphilharmonic_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        # The orchestra's calendar also contains guided walks, talks, and
        # family activities, so records need classification before upload.
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
        return get_concerts()


def main():
    BrusselsPhilharmonicBeCrawler().run()


if __name__ == '__main__':
    main()
