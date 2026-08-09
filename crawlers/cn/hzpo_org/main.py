import re
import time
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'http://www.hzpo.org/'
SOURCE = 'Hangzhou Philharmonic Orchestra'
CITY = 'Hangzhou'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.6',
    'Upgrade-Insecure-Requests': '1',
}

VENUE_TERMS = ('大剧院', '音乐厅', '歌剧院', '剧院', '艺术中心', '文化中心', '演艺中心')
CITY_NAMES = {
    '杭州': 'Hangzhou',
    '上海': 'Shanghai',
    '北京': 'Beijing',
    '宁波': 'Ningbo',
    '绍兴': 'Shaoxing',
    '嘉兴': 'Jiaxing',
    '温州': 'Wenzhou',
    '南京': 'Nanjing',
    '苏州': 'Suzhou',
    '深圳': 'Shenzhen',
    '广州': 'Guangzhou',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(session, url, attempts=4):
    for attempt in range(attempts):
        response = session.get(url, timeout=45)
        if response.status_code != 403 or attempt == attempts - 1:
            response.raise_for_status()
            response.encoding = response.apparent_encoding or 'utf-8'
            return response.text
        time.sleep(2 ** attempt)
    raise RuntimeError(f'Unable to fetch {url}')


def listing_pages(session, section):
    next_url = urljoin(SOURCE_URL, f'{section}.html')
    seen_pages = set()
    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        soup = BeautifulSoup(get_html(session, next_url), 'html.parser')
        yield soup

        page_links = []
        pattern = re.compile(rf'^/{section}/p-(\d+)-(\d+)\.html$')
        for link in soup.select('a[href]'):
            match = pattern.match(urlparse(link.get('href', '')).path)
            if match:
                page_links.append((int(match.group(1)), urljoin(SOURCE_URL, link['href'])))
        next_url = next((url for offset, url in sorted(page_links) if url not in seen_pages), None)


def discover_events(session):
    events = {}
    for section in ('plan', 'past'):
        for soup in listing_pages(session, section):
            for link in soup.select('a[href*="/detail_1/"]'):
                url = urljoin(SOURCE_URL, link.get('href', ''))
                title = clean_text(link.get_text(' ', strip=True))
                if url and title:
                    events[url] = title
    return events


def valid_date(value):
    match = re.search(r'(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})日?', value or '')
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def detail_body(soup):
    candidates = []
    for element in soup.find_all(True):
        classes = element.get('class') or []
        if any(class_name.startswith('e_richText-') for class_name in classes):
            text = clean_text(element)
            if text:
                candidates.append(text)
    return max(candidates, key=len, default='')


def find_venue(description):
    lines = [clean_text(line) for line in description.splitlines() if clean_text(line)]
    for index, line in enumerate(lines):
        candidate = re.sub(r'^地点\s*[：:]\s*', '', line).strip(' ：:')
        if line.startswith('地点') and not candidate and index + 1 < len(lines):
            candidate = lines[index + 1]
        if any(term in candidate for term in VENUE_TERMS):
            if index + 1 < len(lines) and lines[index + 1] in ('歌剧院', '音乐厅'):
                candidate = f'{candidate}·{lines[index + 1]}'
            return candidate
    return None


def find_city(venue):
    for chinese_name, city in CITY_NAMES.items():
        if chinese_name in venue:
            return city
    # The season calendar belongs to a Hangzhou institution and its unnamed
    # home facilities are in Hangzhou. An explicitly named touring city is
    # handled above and is never replaced by this default.
    if venue.startswith(('杭州大剧院', '杭州市')) or venue in ('大剧院', '音乐厅'):
        return CITY
    if venue.startswith(('国家大剧院', '国家艺术中心')):
        return 'Beijing'
    return None


def make_record(url, fallback_title, html):
    soup = BeautifulSoup(html, 'html.parser')
    page_title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    title = re.sub(r'-杭州爱乐乐团$', '', page_title).strip() or fallback_title

    date_element = soup.select_one('[class*="e_timeFormat-"]')
    description = detail_body(soup)
    event_date = valid_date(clean_text(date_element) if date_element else description)
    venue = find_venue(description)
    city = find_city(venue) if venue else None
    if not title or not event_date or not venue or not city:
        return None

    time_match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)', description)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'CN',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = discover_events(session)
    records = []
    for url, title in events.items():
        try:
            record = make_record(url, title, get_html(session, url))
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
        time.sleep(0.75)

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class HzpoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hzpo_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CN',
        upload_target='classical',
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
        return get_concerts()


def main():
    HzpoOrgCrawler().run()


if __name__ == '__main__':
    main()
