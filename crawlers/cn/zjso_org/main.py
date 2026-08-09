import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.zjso.org/'
SOURCE = '浙江交响乐团'
SITEMAP_INDEX_URL = f'{SOURCE_URL}wp-sitemap.xml'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-tribe_events-1.xml'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True) if '<' in value else value
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\\n', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    index = BeautifulSoup(get_response(session, SITEMAP_INDEX_URL).content, 'xml')
    sitemap_urls = [
        loc.get_text(strip=True) for loc in index.find_all('loc')
        if 'sitemap-posts-tribe_events-' in loc.get_text()
    ] or [SITEMAP_URL]
    urls = set()
    for sitemap_url in sitemap_urls:
        sitemap = BeautifulSoup(get_response(session, sitemap_url).content, 'xml')
        urls.update(
            loc.get_text(strip=True) for loc in sitemap.find_all('loc')
            if '/event/' in loc.get_text()
        )
    return sorted(urls)


def event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def city_from_location(location):
    address = location.get('address') or {}
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
        address = clean_text(address.get('streetAddress'))
    else:
        city, address = '', clean_text(address)
    if city:
        return city.removesuffix('市')
    match = re.search(r'([^省市区县]{2,12})市', address)
    return match.group(1) if match else ''


def make_record(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    data = event_data(soup)
    if not data:
        return None
    title = clean_text(data.get('name'))
    location = data.get('location') or {}
    venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
    city = city_from_location(location) if isinstance(location, dict) else ''
    canonical_url = clean_text(data.get('url')) or url
    try:
        start = datetime.fromisoformat(data.get('startDate') or '')
    except (TypeError, ValueError):
        return None
    if not title or not venue or not city or not canonical_url:
        return None
    description_node = soup.select_one('.tribe-events-single-event-description')
    description = clean_text(description_node) or clean_text(data.get('description')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': canonical_url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'CN',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = make_record(url, future.result().text)
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
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title'], item['url']))


class ZjsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='zjso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CN',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ZjsoOrgCrawler().run()


if __name__ == '__main__':
    main()
