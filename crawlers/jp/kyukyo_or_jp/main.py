import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kyukyo.or.jp/'
SOURCE = 'Kyushu Symphony Orchestra'
UPCOMING_URL = urljoin(SOURCE_URL, 'ticket/list.php')
PAST_URL = urljoin(SOURCE_URL, 'ticket/past.php')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The orchestra tours, so its Fukuoka home is not used as a blanket default.
# These are municipality text and first-party venue names observed in the
# current feed and archive. Longer/more specific hints belong first.
CITY_HINTS = {
    '北九州市': 'Kitakyushu',
    '福岡市': 'Fukuoka',
    '久留米市': 'Kurume',
    '飯塚市': 'Iizuka',
    '大牟田市': 'Omuta',
    '宗像市': 'Munakata',
    '春日市': 'Kasuga',
    '太宰府市': 'Dazaifu',
    '直方市': 'Nogata',
    '行橋市': 'Yukuhashi',
    '佐賀市': 'Saga',
    '長崎市': 'Nagasaki',
    '熊本市': 'Kumamoto',
    '大分市': 'Oita',
    '宮崎市': 'Miyazaki',
    '鹿児島市': 'Kagoshima',
    'アクロス福岡': 'Fukuoka',
    '福岡シンフォニーホール': 'Fukuoka',
    '福岡市民ホール': 'Fukuoka',
    '福岡サンパレス': 'Fukuoka',
    'FFGホール': 'Fukuoka',
    '電気ビルみらいホール': 'Fukuoka',
    'キャナルシティ劇場': 'Fukuoka',
    '博多座': 'Fukuoka',
    '大濠公園能楽堂': 'Fukuoka',
    'アクロス円形ホール': 'Fukuoka',
    '末永文化センター': 'Fukuoka',
    'ももちパレス': 'Fukuoka',
    '石橋文化センター': 'Kurume',
    '石橋文化ホール': 'Kurume',
    '久留米シティプラザ': 'Kurume',
    'J:COM北九州芸術劇場': 'Kitakyushu',
    '北九州芸術劇場': 'Kitakyushu',
    '響ホール': 'Kitakyushu',
    'アルモニーサンク': 'Kitakyushu',
    '黒崎ひびしんホール': 'Kitakyushu',
    'ミリカローデン那珂川': 'Nakagawa',
    'ユメニティのおがた': 'Nogata',
    '宗像ユリックス': 'Munakata',
    'イイヅカコスモスコモン': 'Iizuka',
    '大牟田文化会館': 'Omuta',
    '春日市ふれあい文化センター': 'Kasuga',
    '太宰府館': 'Dazaifu',
    '佐賀市文化会館': 'Saga',
    '佐賀県立美術館ホール': 'Saga',
    '長崎ブリックホール': 'Nagasaki',
    '熊本県立劇場': 'Kumamoto',
    '市民会館シアーズホーム夢ホール': 'Kumamoto',
    'iichiko総合文化センター': 'Oita',
    'iichikoグランシアタ': 'Oita',
    'J:COM ホルトホール大分': 'Oita',
    '宮崎市民文化ホール': 'Miyazaki',
    'メディキット県民文化センター': 'Miyazaki',
    '宝山ホール': 'Kagoshima',
    '川商ホール': 'Kagoshima',
    '東京芸術劇場': 'Tokyo',
    'サントリーホール': 'Tokyo',
    '東京オペラシティ': 'Tokyo',
    'ザ・シンフォニーホール': 'Osaka',
}


def clean_text(node):
    if node is None:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', value)
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(
        r'(午前|午後)?\s*([0-2]?\d)\s*時(?:\s*([0-5]?\d)\s*分)?', value
    )
    if not match:
        match = re.search(r'([0-2]?\d)\s*[:：]\s*([0-5]\d)', value)
        if not match:
            return None
        hour, minute = map(int, match.groups())
    else:
        period, raw_hour, raw_minute = match.groups()
        hour, minute = int(raw_hour), int(raw_minute or 0)
        if period == '午後' and hour < 12:
            hour += 12
        elif period == '午前' and hour == 12:
            hour = 0
    return f'{hour:02d}:{minute:02d}' if hour < 24 else None


def resolve_city(venue):
    for hint, city in CITY_HINTS.items():
        if hint in venue:
            return city
    return None


def row_value(article, label):
    for row in article.select('tr'):
        heading = row.find('th')
        if heading and label in clean_text(heading):
            return clean_text(row.find('td'))
    return ''


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    contents = soup.select_one('.contents')
    if not contents:
        return records

    for heading in contents.find_all('h3', recursive=False):
        title_node = heading.select_one('a.title')
        venue_node = heading.find('b', recursive=False)
        article = heading.find_next_sibling('div', class_='article')
        if not all((title_node, venue_node, article)):
            continue

        title = clean_text(title_node).replace('\n', ' ')
        url = urljoin(SOURCE_URL, title_node.get('href', ''))
        venue = clean_text(venue_node).replace('\n', ' ')
        date_text = row_value(article, '開催日')
        event_date = parse_date(date_text)
        city = resolve_city(venue)
        if not all((title, url, venue, event_date, city)):
            continue

        # Listing articles retain the full programme and performers. Remove
        # sales-only material while preserving notes useful for work extraction.
        description_node = BeautifulSoup(str(article), 'html.parser')
        for row in description_node.select('tr'):
            heading_text = clean_text(row.find('th'))
            if 'チケット販売日' in heading_text:
                row.decompose()
        for node in description_node.select('.ticket, .online, script, style'):
            node.decompose()
        description = clean_text(description_node)

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(date_text),
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []

    response = session.get(UPCOMING_URL, timeout=60)
    response.raise_for_status()
    records.extend(parse_listing(response.text))

    # past.php is the first-party archive. It uses stable `paged=N`
    # pagination with five concrete performances per page.
    for page in range(1, 501):
        response = session.get(PAST_URL, params={'paged': page}, timeout=60)
        response.raise_for_status()
        page_records = parse_listing(response.text)
        if not BeautifulSoup(response.text, 'html.parser').select(
            '.contents h3 a.title'
        ):
            break
        records.extend(page_records)
    else:
        log_message(
            'Kyushu Symphony Orchestra archive reached pagination safety limit',
            event='crawler_pagination_limit', level='warning', url=PAST_URL,
            record_count=len(records),
        )

    unique = {}
    for record in records:
        key = (record['url'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class KyukyoOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kyukyo_or_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    KyukyoOrJpCrawler().run()


if __name__ == '__main__':
    main()
