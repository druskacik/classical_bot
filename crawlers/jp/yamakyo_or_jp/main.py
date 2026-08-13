import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.yamakyo.or.jp/'
CONCERT_URL = urljoin(SOURCE_URL, 'concert')
SOURCE = 'Yamagata Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# Touring performances are common. Resolve the venue independently instead of
# applying the orchestra's home city to every event.
CITY_HINTS = {
    '山形市': 'Yamagata', '鶴岡市': 'Tsuruoka', '酒田市': 'Sakata',
    '米沢市': 'Yonezawa', '長井市': 'Nagai', '村山市': 'Murayama',
    '新庄市': 'Shinjo', '天童市': 'Tendo', '寒河江市': 'Sagae',
    '上山市': 'Kaminoyama', '東根市': 'Higashine', '南陽市': 'Nanyo',
    '尾花沢市': 'Obanazawa', '仙台市': 'Sendai', '福島市': 'Fukushima',
    '郡山市': 'Koriyama', '会津若松市': 'Aizuwakamatsu',
    '盛岡市': 'Morioka', '秋田市': 'Akita', '新潟市': 'Niigata',
    '長岡市': 'Nagaoka', '東京都': 'Tokyo', '東京': 'Tokyo',
    '横浜市': 'Yokohama', '横浜': 'Yokohama', '川崎市': 'Kawasaki',
    'さいたま市': 'Saitama', '宇都宮市': 'Utsunomiya', '水戸市': 'Mito',
    '高崎市': 'Takasaki', '長野市': 'Nagano', '松本市': 'Matsumoto',
    '金沢市': 'Kanazawa', '富山市': 'Toyama', '名古屋市': 'Nagoya',
    '名古屋': 'Nagoya', '大阪市': 'Osaka', '大阪': 'Osaka',
    '京都市': 'Kyoto', '京都': 'Kyoto', '神戸市': 'Kobe',
}

HALL_CITIES = {
    '山形テルサ': 'Yamagata', 'やまぎん県民ホール': 'Yamagata',
    '山形市民会館': 'Yamagata', '文翔館': 'Yamagata',
    '山形県郷土館': 'Yamagata', '山形大学': 'Yamagata',
    '荘銀タクト鶴岡': 'Tsuruoka', '庄内町文化創造館響ホール': 'Shonai',
    '酒田市民会館': 'Sakata', '希望ホール': 'Sakata',
    '伝国の杜': 'Yonezawa', '置賜文化ホール': 'Yonezawa',
    '長井市民文化会館': 'Nagai', '村山市民会館': 'Murayama',
    '新庄市民文化会館': 'Shinjo', '天童市市民文化会館': 'Tendo',
    'シェルターなんようホール': 'Nanyo', '河北町総合交流センター': 'Kahoku',
    'ふくしん夢の音楽堂': 'Fukushima', 'けんしん郡山文化センター': 'Koriyama',
    '日立システムズホール仙台': 'Sendai', '仙台銀行ホール': 'Sendai',
    '東京オペラシティ': 'Tokyo', 'サントリーホール': 'Tokyo',
    '東京芸術劇場': 'Tokyo', '紀尾井ホール': 'Tokyo',
    'ミューザ川崎': 'Kawasaki', '横浜みなとみらいホール': 'Yokohama',
}

DETAIL_PATH = re.compile(
    r'^/concert/(?:subscription|shonai|special|others|members|disabled)/[^/?#]+\.html$'
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_city(venue):
    for hint, city in CITY_HINTS.items():
        if hint in venue:
            return city
    for hint, city in HALL_CITIES.items():
        if hint in venue:
            return city
    return None


def listing_page(session, page):
    response = session.get(CONCERT_URL, params={'tab09': page}, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    container = soup.select_one('#tab09')
    if not container:
        raise ValueError('All-concerts tab was not found')
    urls = set()
    for link in container.select('a[href]'):
        absolute = urljoin(CONCERT_URL, link['href'])
        path = requests.utils.urlparse(absolute).path
        if DETAIL_PATH.fullmatch(path):
            urls.add(absolute)
    pages = [
        int(match.group(1))
        for link in container.select('.pagination a[href]')
        if (match := re.search(r'[?&]tab09=(\d+)', link['href']))
    ]
    return urls, max(pages, default=1)


def listing_urls():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_urls, last_page = listing_page(session, 1)
    urls = set(first_urls)
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(listing_page, session, page): page for page in range(2, last_page + 1)}
        for future in as_completed(futures):
            page = futures[future]
            try:
                page_urls, _ = future.result()
                urls.update(page_urls)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Yamagata Symphony Orchestra listing page',
                    event='crawler_listing_fetch_failed', level='warning',
                    url=f'{CONCERT_URL}?tab09={page}', error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(urls)


def parse_dates(value):
    dates = []
    current_year = None
    current_month = None
    pattern = re.compile(r'(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日')
    for match in pattern.finditer(value):
        if match.group(1):
            current_year = int(match.group(1))
        if match.group(2):
            current_month = int(match.group(2))
        if current_year is None or current_month is None:
            continue
        try:
            parsed = date(current_year, current_month, int(match.group(3))).isoformat()
        except ValueError:
            continue
        if parsed not in dates:
            dates.append(parsed)
    return dates


def parse_times(value):
    times = []
    for hour, minute in re.findall(r'([0-2]?\d)\s*[:：]\s*([0-5]\d)\s*開演', value):
        if int(hour) < 24:
            times.append(f'{int(hour):02d}:{minute}')
    return times


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('main .container > h3')
    detail = soup.select_one('main .news-detail')
    if not title_node or not detail:
        return []
    title = clean_text(title_node).replace('\n', ' ')
    date_node = detail.find('h4')
    dates = parse_dates(clean_text(date_node))
    first_free = detail.select_one('.news-detail-free')
    first_paragraph = first_free.find('p') if first_free else None
    lines = [line.strip() for line in clean_text(first_paragraph).splitlines() if line.strip()]
    venue = lines[0] if lines else ''
    city = resolve_city(venue)
    if not all((title, dates, venue, city)):
        return []

    schedule_text = clean_text(first_paragraph)
    times = parse_times(schedule_text)
    if len(times) == 1:
        times *= len(dates)
    description = clean_text(detail)
    records = []
    for index, event_date in enumerate(dates):
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': times[index] if index < len(times) else None,
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url)


def get_concerts():
    urls = listing_urls()
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Yamagata Symphony Orchestra concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class YamakyoOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='yamakyo_or_jp',
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
        return get_concerts()


def main():
    YamakyoOrJpCrawler().run()


if __name__ == '__main__':
    main()
