import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ppt.or.jp/'
SOURCE = 'Pacific Philharmonia Tokyo'
LISTING_URLS = (
    urljoin(SOURCE_URL, 'concerts/'),
    urljoin(SOURCE_URL, 'pastconcerts/'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def listing_urls(session):
    urls = set()
    for listing_url in LISTING_URLS:
        response = session.get(listing_url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.select('a[href*="/concerts/"]'):
            url = canonical_url(urljoin(listing_url, link.get('href', '')))
            path = urlsplit(url).path.rstrip('/')
            if path and path != '/concerts':
                urls.add(url)
    return sorted(urls)


def resolve_city(address, venue):
    text = clean_text(address).replace(' ', '')
    if '東京都' in text:
        return 'Tokyo'

    # Japanese postal addresses normally place the municipality immediately
    # after the prefecture. Keep its first-party spelling instead of guessing a
    # romanization, and retain the ward for designated cities where supplied.
    match = re.search(
        r'(?:都|道|府|県)([^都道府県0-9〒]+?市(?:[^0-9〒]+?区)?|[^都道府県0-9〒]+?区|'
        r'[^都道府県0-9〒]+?[町村])',
        text,
    )
    if match:
        return match.group(1)

    # Defensive fallbacks for the rare detail page whose address modal is
    # absent but whose venue explicitly identifies its municipality.
    hints = {
        '東京': 'Tokyo', '大阪': '大阪市', '姫路': '姫路市', '四万十': '四万十市',
        '練馬': 'Tokyo', '小平': '小平市', '横浜': '横浜市', '神戸': '神戸市',
        '高知': '高知市', '東金': '東金市',
    }
    for hint, city in hints.items():
        if hint in venue:
            return city
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.performance-title')).replace('\n', ' / ')
    venue_node = soup.select_one('.concertdetail-cont .venue p')
    venue = clean_text(venue_node).replace('\n', ' ')
    time_place = clean_text(soup.select_one('.concertdetail-cont .time-place'))
    address = soup.select_one('#map') or soup.select_one('.modal.js-modal')
    city = resolve_city(address, venue)
    if not all((title, venue, time_place, city)):
        return []

    occurrences = []
    date_matches = list(re.finditer(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', time_place))
    for index, match in enumerate(date_matches):
        end = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(time_place)
        time_match = re.search(r'(\d{1,2})[:：](\d{2})\s*開演', time_place[match.end():end])
        occurrences.append((*match.groups(), *(time_match.groups() if time_match else ('', ''))))
    if not occurrences:
        return []

    description_parts = []
    for selector in ('.concertdetail-program', '.concertdetail-about', '.concertdetail-info'):
        text = clean_text(soup.select_one(selector))
        if text:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for year, month, day, hour, minute in occurrences:
        try:
            event_date = date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
        time_from = None
        if hour and int(hour) < 24:
            time_from = f'{int(hour):02d}:{minute}'
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


class PptOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ppt_or_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = listing_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Pacific Philharmonia Tokyo concert detail',
                        event='crawler_detail_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    PptOrJpCrawler().run()


if __name__ == '__main__':
    main()
