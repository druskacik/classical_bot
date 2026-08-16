import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chattanoogasymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}events/sitemap/'
SOURCE = 'Chattanooga Symphony & Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# Most venues in this orchestra's calendar are in Chattanooga. These named
# exceptions are municipalities in its immediate touring/community area.
VENUE_CITIES = {
    'collegedale': 'Collegedale',
    'conn center at lee university': 'Cleveland',
    'first baptist church ringgold': 'Ringgold',
    'ringgold high school': 'Ringgold',
    'signal crest united methodist church': 'Signal Mountain',
    'mountain arts community center': 'Signal Mountain',
    'soddy-daisy': 'Soddy-Daisy',
    'red bank community center': 'Red Bank',
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


def get_response(session, url):
    last_error = None
    for _ in range(3):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
    raise last_error


def event_data(soup):
    for script in soup.find_all('script'):
        text = script.string or script.get_text()
        match = re.search(r'\bvar\s+EVENT\s*=\s*(\{.*?\})\s*;', text, re.S)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return {}
    return {}


def labelled_value(soup, label):
    for heading in soup.select('.mpspx-event-single-body h2'):
        if clean_text(heading).lower() == label.lower():
            value = heading.find_next_sibling('p')
            return clean_text(value)
    return ''


def extract_venue(soup, data):
    venue = clean_text(data.get('attribute_Location'))
    if venue:
        return venue

    after_buy = soup.select_one('.mpspx-event-single-after-buy')
    if after_buy:
        text = clean_text(after_buy)
        match = re.search(r'(?:^|\n)LOCATION\s*\n([^\n]+)', text, re.I)
        if match:
            return clean_text(match.group(1))

    schema = soup.find('script', attrs={'type': 'application/ld+json'}, string=re.compile('startDate'))
    if schema:
        try:
            location = json.loads(schema.string).get('location', {})
            venue = clean_text(location.get('name'))
            if venue.lower() != 'unknown':
                return venue
        except (json.JSONDecodeError, AttributeError):
            pass
    return ''


def extract_description(soup, data):
    parts = []
    embedded = data.get('htmlDescription') or data.get('description')
    if embedded:
        parts.append(clean_text(BeautifulSoup(str(embedded), 'html.parser')))

    inner = soup.select_one('.mpspx-event-single-inner')
    if inner:
        for node in inner.select(':scope > p, :scope > ul, :scope > ol'):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)

    for panel in soup.select('.mpspx-event-single-custom1 .mpspx-panel'):
        text = clean_text(panel)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def city_for_venue(venue):
    normalized = venue.lower()
    for fragment, city in VENUE_CITIES.items():
        if fragment in normalized:
            return city
    return 'Chattanooga'


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    data = event_data(soup)
    title = clean_text(data.get('name'))
    start = clean_text(data.get('start'))
    venue = extract_venue(soup, data)

    if not title or not start or not venue or venue.lower() in {
        'unknown', 'generic venue', 'multiple venues', 'virtual', 'youth orchestra',
    }:
        return None
    try:
        occurrence = datetime.fromisoformat(start)
    except ValueError:
        return None

    return {
        'title': title,
        'date': occurrence.date().isoformat(),
        'url': url,
        'time_from': occurrence.strftime('%H:%M'),
        'venue': venue,
        'city': city_for_venue(venue),
        'country_code': 'US',
        'description': extract_description(soup, data),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def sitemap_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    return [
        clean_text(node)
        for node in soup.find_all('loc')
        if clean_text(node).startswith(f'{SOURCE_URL}events/')
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = sitemap_urls(get_response(session, SITEMAP_URL).content)
    if not urls:
        log_message(
            'No event URLs found in sitemap',
            event='crawler_empty_sitemap',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
        return []

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result().text, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Event page request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ChattSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chattsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChattSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
