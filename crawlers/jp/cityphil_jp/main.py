import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cityphil.jp/'
CONCERT_URL = urljoin(SOURCE_URL, 'concert/')
ARCHIVE_URL = urljoin(CONCERT_URL, 'index_past.php')
SOURCE = 'Tokyo City Philharmonic Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja,en;q=0.7',
}

# The orchestra tours around the Kanto region and occasionally farther afield.
# These venue/place tokens are deliberately more specific than a Tokyo default.
CITY_TOKENS = {
    '東京オペラシティ': 'Tokyo', '東京文化会館': 'Tokyo', '東京芸術劇場': 'Tokyo',
    'サントリーホール': 'Tokyo', 'ティアラこうとう': 'Tokyo', '江東': 'Tokyo',
    '豊洲': 'Tokyo', '亀戸': 'Tokyo', '新宿': 'Tokyo', '練馬': 'Tokyo',
    '板橋': 'Tokyo', '調布': 'Tokyo', '町田': 'Tokyo', '北とぴあ': 'Tokyo',
    '八王子': 'Tokyo', '新国立劇場': 'Tokyo',
    'すみだトリフォニー': 'Tokyo', '紀尾井ホール': 'Tokyo', '文京シビック': 'Tokyo',
    'ミューザ川崎': 'Kawasaki', '川崎': 'Kawasaki', 'ウェスタ川越': 'Kawagoe',
    '川越': 'Kawagoe', '埼玉会館': 'Saitama', '大宮ソニック': 'Saitama',
    '浦安': 'Urayasu', '千葉県文化会館': 'Chiba', '市川': 'Ichikawa',
    '和光': 'Wako', '越谷': 'Koshigaya', '草加': 'Soka', '所沢': 'Tokorozawa',
    '横浜': 'Yokohama', '神奈川県民': 'Yokohama', '鎌倉': 'Kamakura',
    '相模': 'Sagamihara', '群馬音楽センター': 'Takasaki', '高崎': 'Takasaki',
    '栃木文化会館': 'Tochigi', 'とちぎ岩下': 'Tochigi', '宇都宮': 'Utsunomiya',
    '水戸': 'Mito', 'つくば': 'Tsukuba', '山梨': 'Kofu', '長野': 'Nagano',
    '松本': 'Matsumoto', '静岡': 'Shizuoka', '名古屋': 'Nagoya', '大阪': 'Osaka',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def canonical_url(value):
    url = urljoin(CONCERT_URL, value)
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query.rstrip('&'), ''))


def city_for_venue(venue):
    return next((city for token, city in CITY_TOKENS.items() if token in venue), None)


def parse_date(year, value):
    match = re.search(r'(\d{1,2})/(\d{1,2})', value)
    if not match:
        return None
    try:
        return date(year, int(match.group(1)), int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_listing(soup, default_year):
    records = []
    active_year = default_year
    for node in soup.select('.date-table .month_bnr, .date-table section.date-cell'):
        if 'month_bnr' in (node.get('class') or []):
            match = re.search(r'(20\d{2})年', clean_text(node))
            if match:
                active_year = int(match.group(1))
            continue

        link = node.select_one('a.entry-link[href]')
        title = clean_text(node.select_one('.ttl-entry'))
        venue = clean_text(node.select_one('.txt-hall'))
        event_date = parse_date(active_year, clean_text(node.select_one('.date-day')))
        city = city_for_venue(venue)
        if not all((link, title, venue, event_date, city)):
            if venue and not city:
                log_message(
                    'Skipping concert with unresolved city', event='crawler_item_skipped',
                    level='warning', url=canonical_url(link.get('href')) if link else '',
                    error_type='UnresolvedCity', error_message=venue,
                )
            continue

        description_parts = []
        for container in node.select('.cell-date-cont'):
            if container.select_one('.list-dl-contact'):
                continue
            for heading in container.select('.ttl-entry'):
                heading.extract()
            text = clean_text(container)
            if text and text not in description_parts:
                description_parts.append(text)
        records.append({
            'title': title,
            'date': event_date,
            'url': canonical_url(link['href']),
            'time_from': parse_time(clean_text(node.select_one('.date-time'))),
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': '\n\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def archive_years(soup):
    years = {
        int(match.group(1))
        for option in soup.select('#cal-nav-select option')
        if (match := re.search(r'(20\d{2})', clean_text(option)))
    }
    return sorted(years)


def listing_months(soup):
    return sorted({
        match.group(1)
        for option in soup.select('#cal-nav-select option[value]')
        if (match := re.search(r'ym=(20\d{4})', option.get('value', '')))
    })


class CityphilJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cityphil_jp', source=SOURCE, source_url=SOURCE_URL,
        country_code='JP', upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        current = fetch_soup(session, CONCERT_URL)
        archive_index = fetch_soup(session, ARCHIVE_URL)
        records = []
        for year_month in listing_months(current):
            soup = fetch_soup(session, CONCERT_URL, {'ym': year_month})
            records.extend(parse_listing(soup, int(year_month[:4])))
        for year in archive_years(archive_index):
            try:
                soup = archive_index if year == date.today().year else fetch_soup(
                    session, ARCHIVE_URL, {'year': year}
                )
                records.extend(parse_listing(soup, year))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch concert archive', event='crawler_page_failed',
                    level='warning', url=f'{ARCHIVE_URL}?year={year}',
                    error_type=type(error).__name__, error_message=str(error),
                )
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    CityphilJpCrawler().run()


if __name__ == '__main__':
    main()
