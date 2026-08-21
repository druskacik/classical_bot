import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://indianclassical.net/a/faiyaz-khan'
SOURCE = 'Indian Classical Network – Faiyaz Khan'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'india': 'IN',
    'united kingdom': 'GB',
    'uk': 'GB',
    'united states': 'US',
    'united states of america': 'US',
    'usa': 'US',
    'malaysia': 'MY',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = str(value)
        if '<' in text and '>' in text:
            text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    if soup.title and clean_text(soup.title).lower() == 'just a moment...':
        raise requests.HTTPError('Cloudflare challenge page returned', response=response)
    return soup


def json_ld_objects(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                yield item


def event_data(soup):
    for item in json_ld_objects(soup):
        event_type = item.get('@type')
        if event_type == 'Event' or (
            isinstance(event_type, list) and 'Event' in event_type
        ):
            return item
    return None


def country_code(value):
    if isinstance(value, dict):
        value = value.get('name')
    value = clean_text(value)
    if re.fullmatch(r'[A-Za-z]{2}', value):
        return value.upper()
    return COUNTRY_CODES.get(value.lower())


def description_from_page(soup, fallback):
    full = soup.select_one('.field--name-body .readmore-text')
    if full is None:
        full = soup.select_one('.field--name-body')
    if full is not None:
        for link in full.select('.readless-link, .readmore-link'):
            link.decompose()
    return clean_text(full) or clean_text(fallback) or None


def make_record(url, soup):
    data = event_data(soup)
    if not data:
        return None

    title = clean_text(data.get('name'))
    canonical_url = clean_text(data.get('url') or data.get('mainEntityOfPage') or url)
    start = clean_text(data.get('startDate'))
    location = data.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    code = country_code(address.get('addressCountry'))

    try:
        start_at = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except ValueError:
        return None

    if (
        not title
        or not canonical_url
        or urlparse(canonical_url).netloc not in {'indianclassical.net', 'www.indianclassical.net'}
        or not venue
        or not city
        or not code
    ):
        return None

    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': canonical_url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': description_from_page(soup, data.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def performance_urls(soup):
    urls = []
    for article in soup.select('article'):
        link = article.select_one('a[href*="/event/"][href]')
        if link:
            url = urljoin(SOURCE_URL, link['href'])
            if url not in urls:
                urls.append(url)

    # Structured data remains a useful fallback if the card markup changes.
    for item in json_ld_objects(soup):
        performances = item.get('upcomingPerformances') or []
        if isinstance(performances, dict):
            performances = [performances]
        for performance in performances:
            if not isinstance(performance, dict) or not performance.get('url'):
                continue
            url = urljoin(SOURCE_URL, performance['url'])
            if url not in urls:
                urls.append(url)
    return urls


class IndianclassicalNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='indianclassical_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            listing = fetch(session, SOURCE_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Indian Classical Network artist page',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in performance_urls(listing):
            try:
                record = make_record(url, fetch(session, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Indian Classical Network event',
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


def main():
    IndianclassicalNetCrawler().run()


if __name__ == '__main__':
    main()
