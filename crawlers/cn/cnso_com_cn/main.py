import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cnso.com.cn/'
SOURCE = '中国交响乐团'
LIST_URL = urljoin(SOURCE_URL, 'zgjxyt/ychd/xwdt.shtml')

# The site's WAF rejects requests with a generic requests user agent. These are
# the ordinary navigation headers sent by Chromium; no cookie or token is needed.
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
    'Upgrade-Insecure-Requests': '1',
}

CITY_COUNTRIES = {
    '北京': 'CN', '上海': 'CN', '广州': 'CN', '深圳': 'CN', '成都': 'CN',
    '杭州': 'CN', '南京': 'CN', '天津': 'CN', '重庆': 'CN', '武汉': 'CN',
    '西安': 'CN', '长沙': 'CN', '青岛': 'CN', '厦门': 'CN', '苏州': 'CN',
    '珠海': 'CN', '香港': 'HK', '澳门': 'MO', '温哥华': 'CA', '多伦多': 'CA',
    '渥太华': 'CA', '蒙特利尔': 'CA', '河内': 'VN',
}
VENUE_ENDINGS = (
    '音乐厅', '剧院', '剧场', '大会堂', '艺术中心', '文化中心', '歌剧院',
    '体育馆', '音乐堂', '礼堂', 'Concert Hall', 'Theatre', 'Theater',
)
ARTICLE_PATH = re.compile(r'/zgjxyt/ychd/20\d{4}/[^/]+\.shtml$')
FULL_DATE_RE = re.compile(r'(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?')
SHORT_DATE_RE = re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]')
TIME_RE = re.compile(r'(?<!\d)([01]?\d|2[0-3])\s*(?:[:：]\s*([0-5]\d)|点(?:\s*([0-5]\d)\s*分?)?)')


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    if '请求可能存在威胁' in response.text or '请求已被阻断' in response.text:
        raise requests.RequestException('CNSO web application firewall blocked the request')
    return response


def list_page_url(page):
    if page == 1:
        return LIST_URL
    return LIST_URL.replace('.shtml', f'_{page}.shtml')


def article_links(session):
    first = BeautifulSoup(get(session, LIST_URL).content, 'html.parser')
    page_script = ' '.join(script.get_text(' ', strip=True) for script in first.find_all('script'))
    match = re.search(r"createPageHTML\([^,]+,\s*(\d+)", page_script)
    page_count = int(match.group(1)) if match else 1

    soups = [first]
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(get, session, list_page_url(page)) for page in range(2, page_count + 1)]
        for future in as_completed(futures):
            soups.append(BeautifulSoup(future.result().content, 'html.parser'))

    links = set()
    for soup in soups:
        for anchor in soup.find_all('a', href=True):
            url = urljoin(SOURCE_URL, anchor['href'])
            if ARTICLE_PATH.search(url):
                links.add(url)
    return sorted(links)


def valid_date(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def event_dates(text, publication_year, anchor=None):
    values = []
    occupied = []
    for match in FULL_DATE_RE.finditer(text):
        if anchor is not None and abs(match.start() - anchor) > 600:
            continue
        value = valid_date(*match.groups())
        if value and value not in values:
            values.append(value)
        occupied.append(match.span())
    for match in SHORT_DATE_RE.finditer(text):
        if anchor is not None and abs(match.start() - anchor) > 600:
            continue
        if any(start <= match.start() < end for start, end in occupied):
            continue
        value = valid_date(publication_year, *match.groups())
        if value and value not in values:
            values.append(value)
    return values


def event_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or match.group(3) or 0):02d}'


def location(text):
    ending_pattern = '|'.join(map(re.escape, VENUE_ENDINGS))
    patterns = [
        rf'(?:演出地点|演出场地|地点|场地)\s*[:：]\s*([^，。；;\n]{{2,60}}?(?:{ending_pattern}))',
        rf'(?:在|于)\s*([^，。；;\n]{{2,60}}?(?:{ending_pattern}))',
    ]
    venue = ''
    venue_start = 0
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            venue = clean_text(match.group(1)).strip('，,。；; ')
            venue_start = match.start(1)
            break
    if not venue:
        return None

    # Remove temporal or presentational lead-ins accidentally captured before
    # the proper venue name.
    venue = re.sub(r'^.*?(?:晚|上午|下午|日晚|日)\s*', '', venue)
    venue = re.sub(r'^(?:隆重|精彩|正式)?(?:上演|举行)?\s*', '', venue)
    if not venue or len(venue) > 60:
        return None

    context = text[max(0, venue_start - 100):venue_start + len(venue) + 100]
    city = next((name for name in CITY_COUNTRIES if name in venue), None)
    if not city and any(name in venue for name in ('北京音乐厅', '国家大剧院', '中央歌剧院', '中山公园音乐堂', '人民大会堂')):
        city = '北京'
    if not city:
        city = next((name for name in CITY_COUNTRIES if name in context), None)
    if not city and ('在京' in context or '京城' in context):
        city = '北京'
    if not city:
        return None
    return venue, city, CITY_COUNTRIES[city], venue_start


def parse_article(url, content):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('#lbtitle') or soup.select_one('.daew_w1'))
    body_node = soup.select_one('ucapcontent') or soup.select_one('.daew_kuai')
    published = clean_text(soup.select_one('#lbaddtime'))
    body = clean_text(body_node)
    if not title or not body:
        return []
    # Season launches describe dozens of performances, mostly in posters. The
    # surrounding prose also contains press-conference and anniversary dates,
    # which cannot safely be treated as concert dates.
    if re.search(r'(?:发布|公布|启幕).*音乐季', title):
        return []
    year_match = re.search(r'20\d{2}', published) or re.search(r'/((?:20)\d{2})\d{2}/', url)
    if not year_match:
        return []
    publication_year = int(year_match.group(0) if len(year_match.group(0)) == 4 else year_match.group(1))

    resolved_location = location(body)
    if not resolved_location:
        return []
    venue, city, country_code, venue_start = resolved_location
    time_from = event_time(body)
    records = []
    for event_date in event_dates(body, publication_year, venue_start):
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': body,
        })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = article_links(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get, session, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_article(url, future.result().content))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape CNSO article',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']))


class CnsoComCnCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cnso_com_cn',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CN',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CnsoComCnCrawler().run()


if __name__ == '__main__':
    main()
