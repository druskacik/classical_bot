import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://londonconcertchoir.org/'
SOURCE = 'London Concert Choir'
LISTING_URLS = (
    urljoin(SOURCE_URL, 'concerts'),
    urljoin(SOURCE_URL, 'concerts/past'),
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The choir performs mostly in London, but its archive includes tours and
# concerts elsewhere in England. These values are derived from the site's
# first-party venue names and detail-page addresses.
VENUE_GEOGRAPHY = {
    '/venues/basilika-st-ulrich-und-afra-augsburg': ('Augsburg', 'DE'),
    '/venues/bromley-parish-church-kent': ('Bromley', 'GB'),
    '/venues/st-marys-hadleigh': ('Hadleigh', 'GB'),
    '/venues/tonbridge-school-chapel': ('Tonbridge', 'GB'),
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
    return BeautifulSoup(response.content, 'html.parser')


def parse_start(value):
    try:
        return datetime.strptime(value, '%A %d %B %Y, %I.%M%p')
    except (TypeError, ValueError):
        return None


def listing_items(session):
    items = {}
    for listing_url in LISTING_URLS:
        soup = get_soup(session, listing_url)
        cards = soup.select(
            '.view-concerts .view-content > .views-responsive-grid > .row '
            '> [class*="views-column-"]'
        )
        for card in cards:
            title_link = card.select_one('.views-field-title a[href^="/concerts/"]')
            date_element = card.select_one(
                '.views-field-field-date-of-concert .date-display-single'
            )
            venue_link = card.select_one('.views-field-field-concert-venue a[href]')
            if not title_link or not date_element or not venue_link:
                continue

            start = parse_start(clean_text(date_element))
            title = clean_text(title_link)
            venue = clean_text(venue_link)
            if not start or not title or not venue:
                continue

            url = urljoin(SOURCE_URL, title_link.get('href'))
            venue_path = venue_link.get('href')
            city, country_code = VENUE_GEOGRAPHY.get(venue_path, ('London', 'GB'))
            items[url] = {
                'title': title,
                'date': start.date().isoformat(),
                'url': url,
                'time_from': start.strftime('%H:%M'),
                'venue': venue,
                'city': city,
                'country_code': country_code,
            }
    return items


def detail_description(session, url):
    soup = get_soup(session, url)
    content = soup.select_one('#content')
    if not content:
        return None
    for element in content.select(
        'script, style, img, .pdfpreview, .view-concert-downloads'
    ):
        element.decompose()
    return clean_text(content) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, url): url
            for url in items
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                items[url]['description'] = future.result()
            except requests.RequestException as error:
                items[url]['description'] = None
                log_message(
                    'Failed to scrape London Concert Choir event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        items.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class LondonConcertChoirOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='londonconcertchoir_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    LondonConcertChoirOrgCrawler().run()


if __name__ == '__main__':
    main()
