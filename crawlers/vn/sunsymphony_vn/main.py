import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sunsymphony.vn/'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-event-1.xml'
SOURCE = 'Sun Symphony Orchestra'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalBot/1.0)',
    'Accept-Language': 'vi-VN,vi;q=0.9,en;q=0.7',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def event_urls(sitemap_soup):
    urls = []
    seen = set()
    for location in sitemap_soup.find_all('loc'):
        url = clean_text(location)
        parsed = urlparse(url)
        if (
            parsed.netloc in {'sunsymphony.vn', 'www.sunsymphony.vn'}
            and parsed.path.startswith('/event/')
            and url not in seen
        ):
            seen.add(url)
            urls.append(url)
    return urls


def normalize_venue(value):
    folded = value.casefold()
    if 'hồ gươm' in folded or 'ho guom' in folded:
        return 'Nhà hát Hồ Gươm'
    if 'nhà hát lớn hà nội' in folded or 'hanoi opera house' in folded:
        return 'Nhà hát Lớn Hà Nội'
    if 'học viện âm nhạc quốc gia việt nam' in folded:
        if 'phòng hòa nhạc nhỏ' in folded:
            return 'Phòng Hòa nhạc Nhỏ, Học viện Âm nhạc Quốc gia Việt Nam'
        if 'phòng hòa nhạc lớn' in folded or 'khán phòng lớn' in folded:
            return 'Phòng Hòa nhạc Lớn, Học viện Âm nhạc Quốc gia Việt Nam'
        return 'Học viện Âm nhạc Quốc gia Việt Nam'
    return None


def parse_date(value):
    compact = re.sub(r'\s+', '', value)
    try:
        return datetime.strptime(compact, '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', value.strip())
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_detail(soup, url):
    article = soup.select_one('article.single-event')
    title = clean_text(article.select_one('h1')) if article else ''
    event_date = parse_date(clean_text(article.select_one('.date'))) if article else None
    time_from = parse_time(clean_text(article.select_one('.time'))) if article else None
    venue = normalize_venue(clean_text(article.select_one('.locat'))) if article else None
    description = clean_text(article.select_one('.entry-content')) if article else ''

    if not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Hà Nội',
        'country_code': 'VN',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SunSymphonyVnCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sunsymphony_vn',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='VN',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            sitemap_soup = fetch_soup(session, SITEMAP_URL, 'xml')
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Sun Symphony event sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = event_urls(sitemap_soup)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_soup, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = parse_detail(future.result(), url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Sun Symphony event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record is None:
                    log_message(
                        'Skipped Sun Symphony event with incomplete location or date',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                    )
                    continue
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SunSymphonyVnCrawler().run()


if __name__ == '__main__':
    main()
