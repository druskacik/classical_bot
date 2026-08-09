import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nbso.org.cn/'
CONCERT_URL = urljoin(SOURCE_URL, 'concert.html')
SOURCE = 'Ningbo Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.6',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    soup = get_page(session, CONCERT_URL)
    urls = {
        urljoin(SOURCE_URL, anchor.get('href'))
        for anchor in soup.select('a[href*="concert.html?date="]')
        if re.search(r'[?&]date=\d{4}-\d{2}-\d{2}(?:&|$)', anchor.get('href', ''))
    }
    return sorted(urls)


def parse_date_and_time(text):
    normalized = text.replace('：', ':')
    match = re.search(r'(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?', normalized)
    if not match:
        return None, None
    try:
        event_date = date(*(int(value) for value in match.groups())).isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', normalized[match.end():])
    event_time = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, event_time


def parse_venue(text):
    venue = re.sub(r'^\s*(?:地址|地点)\s*[：:]\s*', '', clean_text(text))
    # Street addresses are useful evidence, but do not belong in the venue field.
    venue = re.sub(r'[（(](?:[^）)]*(?:区|路|街|号)[^）)]*)[）)]\s*$', '', venue).strip()
    return venue


def resolve_city(venue):
    if '慈溪' in venue:
        return '慈溪'
    # Zhenhai is a district of Ningbo. All other published venues currently
    # belong to this single-city orchestra's Ningbo concert calendar.
    if venue:
        return '宁波'
    return None


def parse_concert(soup, url):
    details = soup.select_one('.bd-left .item .details')
    if not details:
        return None

    title = clean_text(details.select_one('.title'))
    event_date, event_time = parse_date_and_time(clean_text(details.select_one('.time')))
    venue = parse_venue(details.select_one('.addr'))
    city = resolve_city(venue)

    description_parts = []
    for selector in ('.bd-left .detail .zj-text', '.bd-left .songs .content'):
        part = clean_text(soup.select_one(selector))
        if part and part not in description_parts:
            description_parts.append(part)

    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'CN',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concert(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return parse_concert(get_page(session, url), url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(scrape_concert, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
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
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class NbsoOrgCnCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nbso_org_cn',
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
    NbsoOrgCnCrawler().run()


if __name__ == '__main__':
    main()
