import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operaomaha.org/'
SOURCE = 'Opera Omaha'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
POST_TYPES = ('production', 'event')
HOME_CITY = 'Omaha'

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
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_datetime(value):
    try:
        parsed = datetime.fromisoformat((value or '').replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def city_from_location(value):
    value = clean_text(value)
    # All published locations currently use a US city/state/ZIP ending. Keeping
    # this address-based avoids assigning Omaha to an explicitly touring event.
    match = re.search(
        r'(?:\||\b(?:St(?:reet)?|Ave(?:nue)?|Blvd|Boulevard|Rd|Road|Dr|Drive|'
        r'Ln|Lane|Way|Pkwy|Parkway)\.?)\s+([A-Za-z][A-Za-z .\'-]+),\s*'
        r'[A-Z]{2}\s+\d{5}(?:-\d{4})?\b',
        value,
        re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1))
    if re.search(r'\bOmaha\b', value, re.IGNORECASE):
        return HOME_CITY
    return ''


def venue_from_location(value):
    value = clean_text(value)
    if re.match(r'^\d+(?:st|nd|rd|th)?\s*(?:&|and)\s*\w+', value, re.IGNORECASE):
        return ''
    address = re.search(r'\b\d+\s+(?:[NSEW]\.?\s+)?\d*[A-Za-z][A-Za-z0-9 .\'-]*', value)
    if address:
        return value[:address.start()].strip(' ,|')
    city_state = re.search(r'\b[A-Za-z][A-Za-z .\'-]+,\s*[A-Z]{2}\b', value)
    if city_state:
        return value[:city_state.start()].strip(' ,|')
    return value


def get_posts(session, post_type):
    posts = []
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{post_type}',
            params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return posts
        page += 1


def metadata_value(article, label):
    for item in article.select('.event__meta-item'):
        label_node = item.select_one('.event__meta-label')
        value_node = item.select_one('.event__meta-value')
        if label_node and value_node and clean_text(label_node).lower() == label:
            return clean_text(value_node)
    return ''


def production_records(soup, url):
    article = soup.select_one('article') or soup
    title = clean_text(article.select_one('h1'))
    venue_node = article.select_one('.production-venue')
    location_node = article.select_one('.production-venue__location')
    location = clean_text(location_node)
    venue = ''
    if venue_node:
        venue_copy = BeautifulSoup(str(venue_node), 'html.parser')
        for node in venue_copy.select('.production-venue__location'):
            node.decompose()
        venue = clean_text(venue_copy)
    city = city_from_location(location) or HOME_CITY
    description = clean_text(article)

    records = []
    for time_node in article.select('.production-dates time[datetime]'):
        event_date, time_from = parse_datetime(time_node.get('datetime'))
        if title and event_date and venue and city:
            records.append(make_record(title, event_date, url, time_from, venue, city, description))
    return records


def event_records(soup, url):
    article = soup.select_one('article')
    if not article:
        return []
    title = clean_text(article.select_one('h1'))
    location = metadata_value(article, 'location')
    venue = venue_from_location(location)
    city = city_from_location(location) or HOME_CITY
    description_node = article.select_one('.event__content')
    description = clean_text(description_node) or clean_text(article)

    records = []
    for time_node in article.select('.event__meta time[datetime]'):
        event_date, time_from = parse_datetime(time_node.get('datetime'))
        if title and event_date and venue and city:
            records.append(make_record(title, event_date, url, time_from, venue, city, description))
    return records


def make_record(title, event_date, url, time_from, venue, city, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for post_type in POST_TYPES:
        for post in get_posts(session, post_type):
            url = post.get('link', '')
            if not url.startswith(SOURCE_URL):
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                parser = production_records if post_type == 'production' else event_records
                records.extend(parser(soup, url))
            except requests.RequestException as error:
                log_message(
                    'Could not retrieve Opera Omaha detail page',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No parseable Opera Omaha events found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OperaOmahaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaomaha_org',
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
    OperaOmahaOrgCrawler().run()


if __name__ == '__main__':
    main()
