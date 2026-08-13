import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.societadeiconcerti.it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concerti')
SOURCE = 'Società dei Concerti di Trieste'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
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


def listing_page_count(soup):
    pages = [0]
    for link in soup.select('a[href*="/concerti?page="]'):
        values = parse_qs(urlparse(link.get('href', '')).query).get('page', [])
        if values and values[0].isdigit():
            pages.append(int(values[0]))
    return max(pages) + 1


def event_urls(soup):
    urls = []
    for link in soup.select('.node-event.node-teaser .field-name-title a[href]'):
        url = urljoin(SOURCE_URL, link['href'])
        if url not in urls:
            urls.append(url)
    return urls


def parse_location(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return None

    venue = parts[0]
    city = re.sub(r'^\d{5}\s+', '', parts[-1]).strip()
    country_code = 'IT'
    country_names = {
        'slovenia': 'SI', 'slovenija': 'SI', 'austria': 'AT',
        'croatia': 'HR', 'hrvatska': 'HR',
    }
    folded_city = city.casefold()
    for country_name, code in country_names.items():
        if folded_city.endswith(country_name):
            country_code = code
            city = city[:-len(country_name)].rstrip(' -')
            break

    if not venue or not city or city.casefold() == venue.casefold():
        return None
    return venue, city, country_code


def parse_detail(soup, url):
    event = soup.select_one('.node-event.view-mode-full')
    if event is None:
        return None

    title = clean_text(event.select_one('h1.page-title'))
    date_node = event.select_one('.field-name-field-data [content]')
    location = parse_location(clean_text(event.select_one('.field-name-field-luogo')))
    if not title or date_node is None or not location:
        return None

    date_time = date_node.get('content', '')
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):\d{2}(?:Z|[+-]\d{2}:\d{2})?', date_time)
    if not match:
        return None

    description_parts = []
    for selector in (
        '.field-name-field-sotto-titolo',
        '.field-name-field-abstract',
        '.field-name-field-programma',
    ):
        text = clean_text(event.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)

    venue, city, country_code = location
    return {
        'title': title,
        'date': match.group(1),
        'url': url,
        'time_from': match.group(2),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SocietadeiconcertiItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='societadeiconcerti_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            first_soup = get_soup(session, CALENDAR_URL)
            page_count = listing_page_count(first_soup)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Società dei Concerti calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = []
        for page_number in range(page_count):
            url = CALENDAR_URL if page_number == 0 else f'{CALENDAR_URL}?page={page_number}'
            try:
                soup = first_soup if page_number == 0 else get_soup(session, url)
                for event_url in event_urls(soup):
                    if event_url not in urls:
                        urls.append(event_url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Società dei Concerti listing page',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        for url in urls:
            try:
                record = parse_detail(get_soup(session, url), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Società dei Concerti event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    SocietadeiconcertiItCrawler().run()


if __name__ == '__main__':
    main()
