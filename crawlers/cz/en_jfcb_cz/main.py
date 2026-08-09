import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://en.jfcb.cz/'
CONCERTS_URL = urljoin(SOURCE_URL, 'koncerty')
SOURCE = 'South Czech Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def concert_urls(session):
    soup = get_soup(session, CONCERTS_URL)
    urls = {
        urljoin(SOURCE_URL, link['href']).split('#', 1)[0]
        for link in soup.select('a[href^="/koncerty/"]')
    }
    return sorted(urls)


def parse_datetime(value):
    value = clean_text(value).replace('a.m.', 'AM').replace('p.m.', 'PM')
    try:
        parsed = datetime.strptime(value, '%B %d, %Y, %I:%M %p')
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def resolve_location(venue):
    normalized = venue.casefold()
    if 'vienna' in normalized or 'wien' in normalized:
        return venue, 'Vienna', 'AT'
    if 'chemnitz' in normalized:
        # The source supplies a city but no actual venue for this tour date.
        return None, None, None
    if normalized == 'china':
        return None, None, None

    city_match = re.search(r',\s*([^,]+)$', venue)
    city = clean_text(city_match.group(1)) if city_match else 'České Budějovice'
    return venue, city, 'CZ'


def description_from(soup):
    blocks = []
    for block in soup.select('.rich-text.w-richtext'):
        text = clean_text(block)
        if text and text not in blocks:
            blocks.append(text)
    return '\n\n'.join(blocks) or None


def parse_concert(soup, url):
    title = clean_text(soup.select_one('h1'))
    date_value, time_from = parse_datetime(soup.select_one('.datum-a-cas'))
    venue_text = clean_text(soup.select_one('.opacity-text'))
    venue, city, country_code = resolve_location(venue_text)
    if not title or not date_value or not venue or not city or not country_code:
        return None
    return {
        'title': title,
        'date': date_value,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from(soup),
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_concert(future.result(), url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape South Czech Philharmonic concert detail',
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
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class EnJfcbCzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='en_jfcb_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
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
    EnJfcbCzCrawler().run()


if __name__ == '__main__':
    main()
