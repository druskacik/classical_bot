import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oek.jp/'
SOURCE = 'Orchestra Ensemble Kanazawa'
LIST_URL = urljoin(SOURCE_URL, 'ev_list')
FIRST_ARCHIVE_MONTH = (2018, 4)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Touring performances are common, so explicit event and venue hints are
# resolved before the orchestra's Kanazawa home-venue defaults.
CITY_HINTS = {
    '金沢': 'Kanazawa', '東広島': 'Higashihiroshima', '広島': 'Hiroshima',
    '大阪': 'Osaka', '東京': 'Tokyo', '富山': 'Toyama', '高山': 'Takayama',
    '小松': 'Komatsu', '加賀': 'Kaga', '七尾': 'Nanao', '輪島': 'Wajima',
    '珠洲': 'Suzu', '羽咋': 'Hakui', '白山': 'Hakusan', '野々市': 'Nonoichi',
    '津幡': 'Tsubata', '内灘': 'Uchinada', '能美': 'Nomi', 'かほく': 'Kahoku',
    '穴水': 'Anamizu', '志賀': 'Shika', '中能登': 'Nakanoto',
    '福井': 'Fukui', '敦賀': 'Tsuruga', '鯖江': 'Sabae', '越前': 'Echizen',
    '名古屋': 'Nagoya', '京都': 'Kyoto', '神戸': 'Kobe', '横浜': 'Yokohama',
    '川崎': 'Kawasaki', '札幌': 'Sapporo', '仙台': 'Sendai', '新潟': 'Niigata',
    '長野': 'Nagano', '松本': 'Matsumoto', '岐阜': 'Gifu', '浜松': 'Hamamatsu',
    '岡山': 'Okayama', '福岡': 'Fukuoka', '熊本': 'Kumamoto',
    '石川県立音楽堂': 'Kanazawa', '北國新聞赤羽ホール': 'Kanazawa',
    '金沢歌劇座': 'Kanazawa', '金沢市文化ホール': 'Kanazawa',
    '石川県立能楽堂': 'Kanazawa', '本多の森': 'Kanazawa',
    'ザ・シンフォニーホール': 'Osaka', '東京オペラシティ': 'Tokyo',
    'サントリーホール': 'Tokyo', '紀尾井ホール': 'Tokyo',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_range():
    today = date.today()
    # The public selector exposes roughly the next 15 months. Eighteen months
    # safely covers that moving publication window without relying on its UI.
    end_index = today.year * 12 + today.month - 1 + 18
    start_index = FIRST_ARCHIVE_MONTH[0] * 12 + FIRST_ARCHIVE_MONTH[1] - 1
    return [f'{index // 12:04d}-{index % 12 + 1:02d}' for index in range(start_index, end_index + 1)]


def fetch_month(session, year_month):
    response = session.get(LIST_URL, params={'ym': year_month}, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return {
        urljoin(SOURCE_URL, node['href'])
        for node in soup.select('a[href*="/event/"][href]')
    }


def resolve_city(title, venue):
    evidence = f'{title}\n{venue}'
    for hint, city in CITY_HINTS.items():
        if hint in evidence:
            return city
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    area = soup.select_one('.concertarea')
    if not area:
        return None
    title = clean_text(area.select_one('.con_title')).replace('\n', ' ')
    date_node = area.select_one('.con_date_list time[datetime]')
    venue = clean_text(area.select_one('.con_place')).replace('\n', ' ')
    raw_date = date_node.get('datetime', '') if date_node else ''
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        return None
    city = resolve_city(title, venue)
    if not all((title, venue, city)):
        return None

    time_text = clean_text(area.select_one('.con_time'))
    match = re.search(r'開演\s*([0-2]?\d)\s*[:：]\s*([0-5]\d)', time_text)
    time_from = None
    if match and int(match.group(1)) < 24:
        time_from = f'{int(match.group(1)):02d}:{match.group(2)}'

    description_node = area.select_one('.con_txtarea')
    if description_node:
        for node in description_node.select(
            'script, style, iframe, .ticket, .buy_ticket, .sponcer_table, .data_pdf'
        ):
            node.decompose()
    description = clean_text(description_node)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'JP',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url)


class OekJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oek_jp',
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
        urls = set()
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_month, session, ym): ym for ym in month_range()}
            for future in as_completed(futures):
                ym = futures[future]
                try:
                    urls.update(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape OEK calendar month',
                        event='crawler_listing_fetch_failed', level='warning', url=LIST_URL,
                        year_month=ym, error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape OEK concert detail',
                        event='crawler_detail_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    OekJpCrawler().run()


if __name__ == '__main__':
    main()
