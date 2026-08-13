import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nairobimusicsociety.org/'
SOURCE = 'Nairobi Music Society'
EVENTS_URL = urljoin(SOURCE_URL, 'concerts-recitals-events-nairobi-music/')
EVENTS_API_URL = urljoin(
    SOURCE_URL,
    'wp-json/wp/v2/pages?slug=concerts-recitals-events-nairobi-music',
)
PRODUCTS_API_URL = urljoin(
    SOURCE_URL,
    'wp-json/wp/v2/product?product_cat=33&per_page=100',
)
TIMEOUT = 30

MONTHS = {
    'jan': 1,
    'january': 1,
    'feb': 2,
    'february': 2,
    'mar': 3,
    'march': 3,
    'apr': 4,
    'april': 4,
    'may': 5,
    'jun': 6,
    'june': 6,
    'jul': 7,
    'july': 7,
    'aug': 8,
    'august': 8,
    'sep': 9,
    'sept': 9,
    'september': 9,
    'oct': 10,
    'october': 10,
    'nov': 11,
    'november': 11,
    'dec': 12,
    'december': 12,
}

DATE_PATTERN = re.compile(
    r'(?i)(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?'
    r'\s*,?\s*(\d{1,2})(?:st|nd|rd|th)?\s+'
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|'
    r'dec(?:ember)?)(?:\s*,?\s*(\d{4}))?'
)
TIME_PATTERN = re.compile(r'(?i)\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b')


def clean_text(value):
    return re.sub(r'\s+', ' ', unescape(value or '')).strip()


def parse_occurrence(value, default_year):
    match = DATE_PATTERN.search(value)
    if not match:
        return None

    day = int(match.group(1))
    month = MONTHS[match.group(2).lower().rstrip('.')]
    year = int(match.group(3) or default_year)
    try:
        event_date = datetime(year, month, day).date().isoformat()
    except ValueError:
        return None

    time_match = TIME_PATTERN.search(value)
    time_from = None
    if time_match:
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).lower() == 'p':
            hour += 12
        time_from = f'{hour:02d}:{int(time_match.group(2) or 0):02d}'
    return event_date, time_from


def product_years(session):
    """Map product detail URLs to the year in which each event was published."""
    try:
        response = session.get(PRODUCTS_API_URL, timeout=TIMEOUT)
        response.raise_for_status()
        return {
            product['link'].rstrip('/') + '/': int(product['date'][:4])
            for product in response.json()
            if product.get('link') and product.get('date')
        }
    except (requests.RequestException, ValueError, KeyError) as error:
        log_message(
            'Could not fetch product years; using event page year',
            event='crawler_fetch_warning',
            url=PRODUCTS_API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return {}


def detail_description(session, url):
    if url == EVENTS_URL:
        return None
    try:
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        product = soup.select_one('.product')
        if not product:
            return None
        for unwanted in product.select(
            'form, .cart, .price, .woocommerce-variation-add-to-cart, script, style'
        ):
            unwanted.decompose()
        return clean_text(product.get_text(' ', strip=True)) or None
    except requests.RequestException as error:
        log_message(
            'Could not fetch concert detail',
            event='crawler_fetch_warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class NairobiMusicSocietyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nairobimusicsociety_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='KE',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers['User-Agent'] = (
            'Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)'
        )
        log_message('Fetching event catalogue', event='crawler_url_fetch', url=EVENTS_API_URL)
        response = session.get(EVENTS_API_URL, timeout=TIMEOUT)
        response.raise_for_status()
        pages = response.json()
        if not pages:
            return []

        page = pages[0]
        page_year = int(page['modified'][:4])
        soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
        years_by_url = product_years(session)
        records = []

        for button in soup.select('a.elementor-button'):
            if 'ticket' not in clean_text(button.get_text()).lower():
                continue
            card = button.find_parent('div', class_='e-child')
            if not card:
                continue

            headings = [clean_text(node.get_text(' ', strip=True)) for node in card.select('h3')]
            title = next((heading for heading in headings if heading), None)
            venue_item = next(
                (
                    item
                    for item in card.select('li')
                    if clean_text(item.get_text(' ', strip=True)).lower().startswith('venue:')
                ),
                None,
            )
            if not title or not venue_item:
                continue
            venue = re.sub(
                r'(?i)^venue\s*:\s*',
                '',
                clean_text(venue_item.get_text(' ', strip=True)),
            ).rstrip(' .')
            if not venue:
                continue

            href = clean_text(button.get('href'))
            url = EVENTS_URL if not href or href == '#' else urljoin(SOURCE_URL, href)
            url = url.rstrip('/') + '/'
            default_year = years_by_url.get(url, page_year)

            description_parts = []
            for node in card.select('p'):
                text = clean_text(node.get_text(' ', strip=True))
                if text and text.lower() not in {'venue:', 'tickets'}:
                    description_parts.append(text)
            card_description = clean_text(' '.join(description_parts)) or None
            detail = detail_description(session, url)
            description = detail or card_description

            for item in card.select('li'):
                if item is venue_item:
                    continue
                parsed = parse_occurrence(clean_text(item.get_text(' ', strip=True)), default_year)
                if not parsed:
                    continue
                event_date, time_from = parsed
                records.append(
                    {
                        'title': title,
                        'date': event_date,
                        'url': url,
                        'time_from': time_from,
                        'time_to': None,
                        'venue': venue,
                        'city': 'Nairobi',
                        'description': description,
                    }
                )

        unique = {}
        for record in records:
            key = (
                record['title'],
                record['date'],
                record['time_from'],
                record['venue'],
            )
            unique[key] = record
        return list(unique.values())


def main():
    NairobiMusicSocietyCrawler().run()


if __name__ == '__main__':
    main()
