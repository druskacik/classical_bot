import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bryansymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}event-pages-sitemap.xml'
SOURCE = 'Bryan Symphony Orchestra'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    return [
        clean_text(node)
        for node in soup.find_all('loc')
        if '/event-details/' in clean_text(node)
    ]


def event_json_ld(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed


def parse_location(value):
    if not isinstance(value, dict):
        return '', ''
    venue = clean_text(value.get('name'))
    address = value.get('address')
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
    else:
        match = re.search(r',\s*([^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b', clean_text(address))
        city = clean_text(match.group(1)) if match else ''
    return venue, city


def full_description(soup, fallback):
    section = soup.select_one('[data-hook="about-section"]')
    if not section:
        return clean_text(fallback) or None

    heading = section.find(string=lambda value: value and clean_text(value).lower() == 'about the event')
    if heading:
        heading.extract()
    for node in section.select('button, [data-hook="about-section-button"]'):
        node.decompose()
    description = clean_text(section)
    description = re.sub(r'\n?Show (?:More|Less)\s*$', '', description, flags=re.I).strip()
    return description or clean_text(fallback) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    data = event_json_ld(soup)
    if not data:
        return None

    title = clean_text(data.get('name'))
    start = parse_datetime(data.get('startDate'))
    venue, city = parse_location(data.get('location'))
    if not title or not start or not venue or not city or venue.casefold() == city.casefold():
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': full_description(soup, data.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()

    records = []
    urls = event_urls(response.text)
    for url in urls:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            record = parse_event(detail_response.text, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete structured data',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                )
        except requests.RequestException as error:
            log_message(
                'Event request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    log_message(
        'Bryan Symphony event archive scraped',
        event='crawler_scrape_completed',
        url=SITEMAP_URL,
        record_count=len(records),
    )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BryanSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bryansymphony_org',
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
        return scrape_events()


def main():
    BryanSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
