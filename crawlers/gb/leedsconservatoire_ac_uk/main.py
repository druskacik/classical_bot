import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.leedsconservatoire.ac.uk/'
SOURCE = 'Leeds Conservatoire'
LISTING_URL = urljoin(SOURCE_URL, 'visit-us/whats-on/')
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
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def listing_urls(session):
    soup = get_soup(session, LISTING_URL)
    prefix = urlparse(LISTING_URL).path
    return sorted({
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('a[href]')
        if urlparse(urljoin(SOURCE_URL, link.get('href', ''))).path.startswith(prefix)
        and urlparse(urljoin(SOURCE_URL, link.get('href', ''))).path.rstrip('/')
        != prefix.rstrip('/')
    })


def parse_date(value):
    for pattern in ('%d/%m/%y', '%d/%m/%Y', '%d %B %Y'):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value or '')
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_location(value):
    parts = [part.strip() for part in (value or '').split(',') if part.strip()]
    if len(parts) < 2:
        return None, None

    venue = parts[0]
    city = next(
        (
            name
            for part in parts[1:]
            for name in ('Leeds', 'London')
            if re.search(rf'\b{re.escape(name)}\b', part, re.I)
        ),
        None,
    )
    if not city:
        postcode_index = next(
            (index for index, part in enumerate(parts) if re.search(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', part, re.I)),
            None,
        )
        if postcode_index and postcode_index >= 2:
            city = parts[postcode_index - 1]
    return venue, city


def detail_record(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('.hero-content h1'))
    details = soup.select_one('.key-details')
    date = parse_date(clean_text(details.select_one('time.date'))) if details else None
    time_from = parse_time(clean_text(details.select_one('time.time'))) if details else None
    location = clean_text(details.select_one('address')) if details else ''
    venue, city = parse_location(location)

    content = soup.select_one('#book')
    if content:
        for element in content.select('script, style, .entry, a.button'):
            element.decompose()
    description = clean_text(content) or clean_text(soup.select_one('.hero-content p')) or None

    if not all((title, date, venue, city)):
        log_message(
            'Skipping Leeds Conservatoire event with incomplete details',
            event='crawler_item_skipped',
            level='warning',
            url=url,
        )
        return None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LeedsConservatoireAcUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='leedsconservatoire_ac_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        records = []
        for url in listing_urls(session):
            try:
                record = detail_record(session, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Leeds Conservatoire event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    LeedsConservatoireAcUkCrawler().run()


if __name__ == '__main__':
    main()
